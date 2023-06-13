import copy
import json
import os
import random
import warnings
from collections import OrderedDict
from communication.log_communication import log_communication

import flwr as fl
import numpy as np
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm

from build_utils import (build_dataset, build_model, build_optimizer, build_provider_dataset)
from datasets.DocILE_ELSA import collate_fn
from differential_privacy.dp_utils import (add_dp_noise, clip_parameters, flatten_params, get_shape, reconstruct)
from eval import evaluate  # fl_centralized_evaluation
from logger import Logger
from metrics import Evaluator
from utils import load_config, parse_args, seed_everything
from utils_parallel import get_parameters_from_model, set_parameters_model, weighted_average
from collections import OrderedDict


def fl_train(data_loaders, model, optimizer, lr_scheduler, evaluator, logger, client_id, fl_config):
    """
    Trains and returns the updated weights.
    """
    model.model.train()
    param_keys = list(model.model.state_dict().keys())
    parameters = copy.deepcopy(list(model.model.state_dict().values()))

    warnings.warn("\n\n" + str(fl_config) + "\n\n")
    agg_update = None

    if not config.use_dp and len(data_loaders) > 1:
        raise ValueError("Non private training should only use one data loader.")

    warnings.warn("LEN: " + str(len(data_loaders)))
    tqdm_multiplier = len(data_loaders) * config.fl_parameters.iterations_per_fl_round
    for provider_dataloader in data_loaders:
        # total_loss = 0

        # set model weights to state of beginning of federated round
        state_dict = OrderedDict({k: v for k, v in zip(param_keys, parameters)})
        model.model.load_state_dict(state_dict, strict=True)
        model.model.train()

        # perform n provider iterations (each provider has their own dataloader in the non-private case)
        for iter in range(config.fl_parameters.iterations_per_fl_round):
            for batch_idx, batch in enumerate(tqdm(provider_dataloader)):

                gt_answers = batch['answers']
                outputs, pred_answers, pred_answer_page, answer_conf = model.forward(batch, return_pred_answer=True)
                loss = outputs.loss + outputs.ret_loss if hasattr(outputs, 'ret_loss') else outputs.loss

                # total_loss += loss.item() / len(batch['question_id'])
                loss.backward()
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

                metric = evaluator.get_metrics(gt_answers, pred_answers)

                batch_acc = np.mean(metric['accuracy'])
                batch_anls = np.mean(metric['anls'])

                log_dict = {
                    'Train/Batch loss': outputs.loss.item(),
                    'Train/Batch Accuracy': batch_acc,
                    'Train/Batch ANLS': batch_anls,
                    'lr': optimizer.param_groups[0]['lr']
                }

                if hasattr(outputs, 'ret_loss'):
                    log_dict['Train/Batch retrieval loss'] = outputs.ret_loss.item()

                if 'answer_page_idx' in batch and None not in batch['answer_page_idx']:
                    ret_metric = evaluator.get_retrieval_metric(batch.get('answer_page_idx', None), pred_answer_page)
                    batch_ret_prec = np.mean(ret_metric)
                    log_dict['Train/Batch Ret. Prec.'] = batch_ret_prec

                logger.logger.log(log_dict)

        # After all the iterations:
        # Get the update
        new_update = [w - w_0 for w, w_0 in zip(list(model.model.state_dict().values()), parameters)]  # Get model update

        if config.use_dp:
            # flatten update
            shapes = get_shape(new_update)
            new_update = flatten_params(new_update)

            # clip update:
            new_update = clip_parameters(new_update, clip_norm=config.dp_params.sensitivity)

            # Aggregate (Avg)
            if agg_update is None:
                agg_update = new_update
            else:
                agg_update += new_update

    # Handle DP after all updates are done
    if config.use_dp:
        # Add the noise
        agg_update = add_dp_noise(agg_update, noise_multiplier=config.dp_params.noise_multiplier, sensitivity=config.dp_params.sensitivity)

        # Divide the noisy aggregated update by the number of providers (100)
        agg_update = torch.div(agg_update, len(data_loaders))

        # Add the noisy update to the original model
        agg_update = reconstruct(agg_update, shapes)
    else:
        agg_update = new_update

    upd_weights = [torch.add(agg_upd, w_0).cpu() for agg_upd, w_0 in zip(agg_update, copy.deepcopy(parameters))]

    logger.logger.log(log_dict, step=logger.current_epoch * logger.len_dataset + batch_idx)

    # if fl_config["log_path"] is not None:
    if config.flower:
        log_communication(federated_round=fl_config["current_round"], sender=client_id, receiver=-1, data=parameters, log_location=fl_config["log_path"])
        
    # Send the weights to the server
    return upd_weights


class FlowerClient(fl.client.NumPyClient):
    def __init__(self, model, trainloader, valloader, optimizer, lr_scheduler, evaluator, logger, config, client_id):
        self.model = model
        self.trainloader = trainloader
        self.valloader = valloader
        self.optimizer = optimizer
        self.lr_scheduler = lr_scheduler
        self.evaluator = evaluator
        self.logger = logger
        self.logger.log_model_parameters(self.model)
        self.config = config
        self.client_id = client_id

    def fit(self, parameters, config):
        self.set_parameters(self.model, parameters, config)
        updated_weigths = fl_train(self.trainloader, self.model, self.optimizer, self.lr_scheduler, self.evaluator, self.logger, self.client_id, config)
        return updated_weigths, 1, {}  # TODO 1 ==> Number of selected clients.

    def set_parameters(self, model, parameters, config):
        params_dict = zip(model.model.state_dict().keys(), parameters)
        state_dict = OrderedDict({k: torch.Tensor(v) for k, v in params_dict})
        if config["log_path"] is not None:
            log_communication(federated_round=config["current_round"], sender=-1, receiver=self.client_id, data=parameters, log_location=config["log_path"])
            
        model.model.load_state_dict(state_dict, strict=True)

    # The `evaluate` function will be by Flower called after every round
    def evaluate(self, parameters, config):
        set_parameters_model(self.model, parameters)
        accuracy, anls, ret_prec, _, _ = evaluate(self.valloader, self.model, self.evaluator, config)  # data_loader, model, evaluator, **kwargs
        is_updated = self.evaluator.update_global_metrics(accuracy, anls, 0)
        self.logger.log_val_metrics(accuracy, anls, ret_prec, update_best=is_updated)

        return float(0), len(self.valloader), {"accuracy": float(accuracy), "anls": anls}


def client_fn(client_id):
    """Create a Flower client representing a single organization."""
    # Create a list of train data loaders with one dataloader per provider

    if config.use_dp:
        # Pick a subset of providers
        provider_to_doc = json.load(open(config.provider_docs, 'r'))
        provider_to_doc = provider_to_doc["client_" + client_id]
        providers = random.sample(list(provider_to_doc.keys()), k=config.dp_params.providers_per_fl_round)  # 50
        train_datasets = [build_provider_dataset(config, 'train', provider_to_doc, provider, client_id) for provider in providers]
        
    else:
        train_datasets = [build_dataset(config, 'train', client_id=client_id)]

    train_data_loaders = [DataLoader(train_dataset, batch_size=config.batch_size, shuffle=False, collate_fn=collate_fn) for train_dataset in train_datasets]
    total_training_steps = [len(data_loader) for data_loader in train_data_loaders]

    # Create validation data loader
    val_dataset = build_dataset(config, 'val')
    val_data_loader = DataLoader(val_dataset, batch_size=config.batch_size, shuffle=False, collate_fn=collate_fn)

    optimizer, lr_scheduler = build_optimizer(model, length_train_loader=len(total_training_steps), config=config)

    evaluator = Evaluator(case_sensitive=False)
    logger = Logger(config=config)
    return FlowerClient(model, train_data_loaders, val_data_loader, optimizer, lr_scheduler, evaluator, logger, config, client_id)


def get_on_fit_config_fn(log_path):
    """Return a function which returns training configurations."""

    def fit_config(server_round: int):
        """Return training configuration dict for each round."""
        config = {
            "current_round": server_round,
            "log_path": log_path
        }
        return config

    return fit_config


# def get_on_eval_config_fn(config):
#     """Return a function which returns training configurations."""
#
#     def evaluate_config(server_round: int):
#         """Return evaluate configuration dict for each round."""
#         config.current_round = server_round
#
#         return config
#
#     return evaluate_config

def evaluate_config(server_round: int):
    """Return evaluate configuration dict for each round."""
    config.current_round = server_round

    return config


if __name__ == '__main__':
    args = parse_args()
    config = load_config(args)
    seed_everything(config.seed)

    # Set `MASTER_ADDR` and `MASTER_PORT` environment variables
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '9957'

    NUM_CLIENTS = config.fl_parameters["num_clients"]
    model = build_model(config)
    params = get_parameters_from_model(model)

    # Create FedAvg strategyq
    strategy = fl.server.strategy.FedAvg(
        # fraction_fit=config.dp_params.client_sampling_probability,  # Sample 100% of available clients for training
        fraction_fit=0.33,  # Sample 100% of available clients for training
        fraction_evaluate=1,  # Sample 50% of available clients for evaluation
        min_fit_clients=NUM_CLIENTS,  # Never sample less than 10 clients for training
        min_evaluate_clients=NUM_CLIENTS,  # Never sample less than 5 clients for evaluation
        min_available_clients=NUM_CLIENTS,  # Wait until all 10 clients are available
        # fit_metrics_aggregation_fn=weighted_average,  # <-- pass the metric aggregation function
        evaluate_metrics_aggregation_fn=weighted_average,  # <-- pass the metric aggregation function
        initial_parameters=fl.common.ndarrays_to_parameters(params),
        # on_fit_config_fn=get_on_fit_config_fn(config.log_path),
        on_fit_config_fn=get_on_fit_config_fn(config.log_path),  # Log path hardcoded according to /save dir
        # evaluate_fn=fl_centralized_evaluation,  # Pass the centralized evaluation function
        on_evaluate_config_fn=evaluate_config,
        # on_evaluate_config_fn=get_on_eval_config_fn(config),
    )

    # Specify client resources if you need GPU (defaults to 1 CPU and 0 GPU)
    client_resources = None
    if config.device == "cuda":
        client_resources = {"num_gpus": 1}  # TODO Check number of GPUs

    # Start simulation
    fl.simulation.start_simulation(
        client_fn=client_fn,
        num_clients=NUM_CLIENTS,
        config=fl.server.ServerConfig(num_rounds=config.fl_parameters["num_rounds"]),
        strategy=strategy,
        client_resources=client_resources,
        ray_init_args={"local_mode": True}  # run in one process to avoid zombie ray processes
    )

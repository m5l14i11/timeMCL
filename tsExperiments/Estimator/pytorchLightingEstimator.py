"""The original class for Estimation from GluonTS."""
# See https://ts.gluon.ai/stable/_modules/gluonts/torch/model/estimator.html#PyTorchLightningEstimator

from typing import NamedTuple, Optional, Iterable, Dict, Any
import logging

import numpy as np
import lightning.pytorch as pl
import torch.nn as nn

from gluonts.core.component import validated
from gluonts.dataset.common import Dataset
from gluonts.env import env
from gluonts.itertools import Cached
from gluonts.model import Estimator, Predictor
from gluonts.torch.model.predictor import PyTorchPredictor
from gluonts.transform import Transformation

logger = logging.getLogger(__name__)


class TrainOutput(NamedTuple):
    transformation: Transformation
    trained_net: nn.Module
    trainer: pl.Trainer
    predictor: PyTorchPredictor


class PyTorchLightningEstimator(Estimator):
    """
    An `Estimator` type with utilities for creating PyTorch-Lightning-based
    models.

    To extend this class, one needs to implement three methods:
    `create_transformation`, `create_training_network`, `create_predictor`,
    `create_training_data_loader`, and `create_validation_data_loader`.
    """

    @validated()
    def __init__(
        self,
        trainer_kwargs: Dict[str, Any],
        lead_time: int = 0,
    ) -> None:
        super().__init__(lead_time=lead_time)
        self.trainer_kwargs = trainer_kwargs

    def create_transformation(self) -> Transformation:
        """
        Create and return the transformation needed for training and inference.

        Returns
        -------
        Transformation
            The transformation that will be applied entry-wise to datasets,
            at training and inference time.
        """
        raise NotImplementedError

    def create_lightning_module(self) -> pl.LightningModule:
        """
        Create and return the network used for training (i.e., computing the
        loss).

        Returns
        -------
        pl.LightningModule
            The network that computes the loss given input data.
        """
        raise NotImplementedError

    def create_predictor(
        self,
        transformation: Transformation,
        module,
    ) -> PyTorchPredictor:
        """
        Create and return a predictor object.

        Parameters
        ----------
        transformation
            Transformation to be applied to data before it goes into the model.
        module
            A trained `pl.LightningModule` object.

        Returns
        -------
        Predictor
            A predictor wrapping a `nn.Module` used for inference.
        """
        raise NotImplementedError

    def create_training_data_loader(self, data: Dataset, module, **kwargs) -> Iterable:
        """
        Create a data loader for training purposes.

        Parameters
        ----------
        data
            Dataset from which to create the data loader.
        module
            The `pl.LightningModule` object that will receive the batches from
            the data loader.

        Returns
        -------
        Iterable
            The data loader, i.e. and iterable over batches of data.
        """
        raise NotImplementedError

    def create_validation_data_loader(
        self, data: Dataset, module, **kwargs
    ) -> Iterable:
        """
        Create a data loader for validation purposes.

        Parameters
        ----------
        data
            Dataset from which to create the data loader.
        module
            The `pl.LightningModule` object that will receive the batches from
            the data loader.

        Returns
        -------
        Iterable
            The data loader, i.e. and iterable over batches of data.
        """
        raise NotImplementedError

    def train_model(
        self,
        training_data: Dataset,
        validation_data: Optional[Dataset] = None,
        from_predictor: Optional[PyTorchPredictor] = None,
        shuffle_buffer_length: Optional[int] = None,
        cache_data: bool = False,
        ckpt_path: Optional[str] = None,
        **kwargs,
    ) -> TrainOutput:
        transformation = self.create_transformation()

        with env._let(max_idle_transforms=max(len(training_data), 100)):
            transformed_training_data: Dataset = transformation.apply(
                training_data, is_train=True
            )
            if cache_data:
                transformed_training_data = Cached(transformed_training_data)

            training_network = self.create_lightning_module()

            training_data_loader = self.create_training_data_loader(
                transformed_training_data,
                training_network,
                shuffle_buffer_length=shuffle_buffer_length,
            )

        validation_data_loader = None

        if validation_data is not None:
            with env._let(max_idle_transforms=max(len(validation_data), 100)):
                transformed_validation_data: Dataset = transformation.apply(
                    validation_data, is_train=True
                )
                if cache_data:
                    transformed_validation_data = Cached(transformed_validation_data)

                validation_data_loader = self.create_validation_data_loader(
                    transformed_validation_data,
                    training_network,
                )

        if from_predictor is not None:
            training_network.load_state_dict(from_predictor.network.state_dict())

        monitor = "train_loss" if validation_data is None else "val_loss"
        checkpoint = pl.callbacks.ModelCheckpoint(
            monitor=monitor, mode="min", verbose=True
        )
        custom_callbacks = self.trainer_kwargs.pop("callbacks", [])

        if (
            validation_data is None
        ):  # in this case, we only want to save the last model, and we use the checkpoint callback
            all_callbacks = [checkpoint] + custom_callbacks
        else:  # in this case, we want to save the best model (and optionally the last model), and we use our own callback
            all_callbacks = custom_callbacks

        validation_only = self.trainer_kwargs.pop("validation_only", False)
        # is_tactis = self.trainer_kwargs.pop("is_tactis", False)

        trainer = pl.Trainer(
            **{
                "accelerator": "auto",
                "callbacks": all_callbacks,
                "logger": self.trainer_kwargs.pop("logger", None),
                **self.trainer_kwargs,
            }
        )

        if validation_only:
            logger.info("Skipping training and only validating")
            trainer.validate(
                model=training_network,
                dataloaders=validation_data_loader,
                ckpt_path=ckpt_path,
            )
            best_model = training_network
        else:
            trainer.fit(
                model=training_network,
                train_dataloaders=training_data_loader,
                val_dataloaders=validation_data_loader,
                ckpt_path=ckpt_path,
            )

            # if is_tactis:
            #     best_model = training_network
            # else:
            if checkpoint.best_model_path != "":
                logger.info(f"Loading best model from {checkpoint.best_model_path}")
                best_model = training_network.__class__.load_from_checkpoint(
                    checkpoint.best_model_path
                )
            else:
                best_model = training_network

        return TrainOutput(
            transformation=transformation,
            trained_net=best_model,
            trainer=trainer,
            predictor=self.create_predictor(transformation, best_model),
        )

    @staticmethod
    def _worker_init_fn(worker_id):
        np.random.seed(np.random.get_state()[1][0] + worker_id)

    def train(
        self,
        training_data: Dataset,
        validation_data: Optional[Dataset] = None,
        shuffle_buffer_length: Optional[int] = None,
        cache_data: bool = False,
        ckpt_path: Optional[str] = None,
        **kwargs,
    ) -> PyTorchPredictor:
        return self.train_model(
            training_data,
            validation_data,
            shuffle_buffer_length=shuffle_buffer_length,
            cache_data=cache_data,
            ckpt_path=ckpt_path,
        ).predictor

    def train_from(
        self,
        predictor: Predictor,
        training_data: Dataset,
        validation_data: Optional[Dataset] = None,
        shuffle_buffer_length: Optional[int] = None,
        cache_data: bool = False,
        ckpt_path: Optional[str] = None,
    ) -> PyTorchPredictor:
        assert isinstance(predictor, PyTorchPredictor)
        return self.train_model(
            training_data,
            validation_data,
            from_predictor=predictor,
            shuffle_buffer_length=shuffle_buffer_length,
            cache_data=cache_data,
            ckpt_path=ckpt_path,
        ).predictor

# This code was adapted from https://github.com/zalandoresearch/pytorch-ts/blob/master/pts/model/time_grad/time_grad_network.py
# under MIT License

import torch.nn as nn
from typing import List, Optional, Union, Tuple
from tsExperiments.models.project_models.timeGrad.utils import (
    GaussianDiffusion,
    DiffusionOutput,
    EpsilonTheta,
    weighted_average,
)
from data_and_transformation import (
    MeanScaler,
    NOPScaler,
    MeanStdScaler,
    CenteredMeanScaler,
)
import torch
from gluonts.model import Input, InputSpec


class PersonnalizedTimeGrad(nn.Module):
    def __init__(
        self,
        num_parallel_samples: int,
        num_layers: int,
        num_cells: int,
        cell_type: str,
        context_length: int,
        prediction_length: int,
        dropout_rate: float,
        target_dim: int,
        conditioning_length: int,
        diff_steps: int,
        loss_type: str,
        beta_end: float,
        beta_schedule: str,
        residual_layers: int,
        residual_channels: int,
        dilation_cycle_length: int,
        embedding_dimension: int,
        scaling: bool,
        num_feat_dynamic_real: int,
        lags_seq: List[int],
        scaler_type="mean",
        div_by_std=False,
        minimum_std=1e-3,
        minimum_std_cst=1e-4,
        default_scale=True,
        default_scale_cst=True,
        add_minimum_std=True,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.scaler_type = scaler_type
        self.div_by_std = div_by_std
        self.minimum_std = minimum_std
        self.minimum_std_cst = minimum_std_cst
        self.default_scale = default_scale
        self.default_scale_cst = default_scale_cst
        self.add_minimum_std = add_minimum_std
        self.lags_seq = lags_seq
        self.history_length = context_length + max(self.lags_seq)  # 192

        self.target_dim = target_dim
        self.prediction_length = prediction_length
        self.context_length = context_length
        self.scaling = scaling
        self.num_parallel_samples = num_parallel_samples

        # for decoding the lags are shifted by one,
        # at the first time-step of the decoder a lag of one corresponds to
        # the last target value
        self.shifted_lags = [l - 1 for l in self.lags_seq]

        self.lags_seq.sort()

        self.num_feat_dynamic_real = num_feat_dynamic_real

        self.denoise_fn = EpsilonTheta(
            target_dim=target_dim,
            cond_length=conditioning_length,
            residual_layers=residual_layers,
            residual_channels=residual_channels,
            dilation_cycle_length=dilation_cycle_length,
        )

        self.diffusion = GaussianDiffusion(
            self.denoise_fn,
            input_size=target_dim,
            diff_steps=diff_steps,
            loss_type=loss_type,
            beta_end=beta_end,
            beta_schedule=beta_schedule,
        )

        self.distr_output = DiffusionOutput(
            self.diffusion, input_size=target_dim, cond_size=conditioning_length
        )

        self.proj_dist_args = self.distr_output.get_args_proj(num_cells)

        self.embed_dim = embedding_dimension
        self.embed = nn.Embedding(
            num_embeddings=self.target_dim, embedding_dim=self.embed_dim
        )

        if self.scaling:
            if self.scaler_type == "mean":
                self.scaler = MeanScaler(keepdim=True)
            elif self.scaler_type == "nops":
                self.scaler = NOPScaler(keepdim=True)
            elif self.scaler_type == "centered_mean":
                self.scaler = CenteredMeanScaler(keepdim=True)
            elif self.scaler_type == "mean_std":
                self.scaler = MeanStdScaler(
                    minimum_std=self.minimum_std,
                    minimum_std_cst=self.minimum_std_cst,
                    default_scale=self.default_scale,
                    default_scale_cst=self.default_scale_cst,
                    add_minimum_std=self.add_minimum_std,
                    keepdim=True,
                )
            else:
                raise ValueError(f"Invalid scaler type: {self.scaler_type}")
        else:
            self.scaler = NOPScaler(keepdim=True)

        # Calculate input_size adaptively
        self.input_size = (
            len(self.lags_seq) * self.target_dim
            + self.target_dim * self.embed_dim
            + self.num_feat_dynamic_real
        )

        print(f"Setting input_size to {self.input_size}")

        self.cell_type = cell_type
        rnn_cls = {"LSTM": nn.LSTM, "GRU": nn.GRU}[cell_type]
        self.rnn = rnn_cls(
            input_size=self.input_size,
            hidden_size=num_cells,
            num_layers=num_layers,
            dropout=dropout_rate,
            batch_first=True,
        )

    def describe_inputs(self, batch_size=1) -> InputSpec:
        return InputSpec(
            {
                "target_dimension_indicator": Input(
                    shape=(batch_size, self.target_dim),
                    dtype=torch.long,
                ),
                "past_target_cdf": Input(
                    shape=(batch_size, self.history_length, self.target_dim),
                    dtype=torch.float,
                ),
                "past_observed_values": Input(
                    shape=(batch_size, self.history_length, self.target_dim),
                    dtype=torch.float,
                ),
                "past_is_pad": Input(
                    shape=(batch_size, self.history_length), dtype=torch.float
                ),
                "future_time_feat": Input(
                    shape=(
                        batch_size,
                        self.prediction_length,
                        self.num_feat_dynamic_real,
                    ),
                    dtype=torch.float,
                ),
                "past_time_feat": Input(
                    shape=(
                        batch_size,
                        self.history_length,
                        self.num_feat_dynamic_real,
                    ),
                    dtype=torch.float,
                ),
                "future_target_cdf": Input(
                    shape=(batch_size, self.prediction_length, self.target_dim),
                    dtype=torch.float,
                ),
                "future_observed_values": Input(
                    shape=(batch_size, self.prediction_length, self.target_dim),
                    dtype=torch.float,
                ),
            },
            zeros_fn=torch.zeros,
        )

    @staticmethod
    def get_lagged_subsequences(
        sequence: torch.Tensor,
        sequence_length: int,
        indices: List[int],
        subsequences_length: int = 1,
    ) -> torch.Tensor:
        """
        Returns lagged subsequences of a given sequence.
        Parameters
        ----------
        sequence
            the sequence from which lagged subsequences should be extracted.
            Shape: (N, T, C).
        sequence_length
            length of sequence in the T (time) dimension (axis = 1).
        indices
            list of lag indices to be used.
        subsequences_length
            length of the subsequences to be extracted.
        Returns
        --------
        lagged : Tensor
            a tensor of shape (N, S, C, I),
            where S = subsequences_length and I = len(indices),
            containing lagged subsequences.
            Specifically, lagged[i, :, j, k] = sequence[i, -indices[k]-S+j, :].
        """
        # we must have: history_length + begin_index >= 0
        # that is: history_length - lag_index - sequence_length >= 0
        # hence the following assert
        assert max(indices) + subsequences_length <= sequence_length, (
            f"lags cannot go further than history length, found lag "
            f"{max(indices)} while history length is only {sequence_length}"
        )
        assert all(lag_index >= 0 for lag_index in indices)

        lagged_values = []
        for lag_index in indices:
            begin_index = -lag_index - subsequences_length
            end_index = -lag_index if lag_index > 0 else None
            lagged_values.append(sequence[:, begin_index:end_index, ...].unsqueeze(1))
        return torch.cat(lagged_values, dim=1).permute(0, 2, 3, 1)

    def unroll(
        self,
        lags: torch.Tensor,
        scale_params: dict,
        time_feat: torch.Tensor,
        target_dimension_indicator: torch.Tensor,
        unroll_length: int,
        begin_state: Optional[Union[List[torch.Tensor], torch.Tensor]] = None,
    ) -> Tuple[
        torch.Tensor,
        Union[List[torch.Tensor], torch.Tensor],
        torch.Tensor,
        torch.Tensor,
    ]:
        if self.scaler_type == "mean":
            scale = scale_params["scale"]
            # (batch_size, sub_seq_len, target_dim, num_lags)
            lags_scaled = lags / scale.unsqueeze(-1)
            # assert_shape(
            #     lags_scaled, (-1, unroll_length, self.target_dim, len(self.lags_seq)),
            # )
        elif self.scaler_type == "mean_std" or self.scaler_type == "centered_mean":
            mean = scale_params["mean"]
            std = scale_params["std"]
            lags_scaled = (lags - mean.unsqueeze(-1)) / std.unsqueeze(-1)
        else:
            raise ValueError(f"Invalid scaler type: {self.scaler_type}")

        # (batch_size, sub_seq_len, target_dim, num_lags)
        # lags_scaled = lags / scale.unsqueeze(-1)

        # assert_shape(
        #     lags_scaled, (-1, unroll_length, self.target_dim, len(self.lags_seq)),
        # )

        input_lags = lags_scaled.reshape(
            (-1, unroll_length, len(self.lags_seq) * self.target_dim)
        )

        if self.embed_dim > 0:
            # (batch_size, target_dim, embed_dim)
            index_embeddings = self.embed(target_dimension_indicator)
            # assert_shape(index_embeddings, (-1, self.target_dim, self.embed_dim))

            # (batch_size, seq_len, target_dim * embed_dim)
            repeated_index_embeddings = (
                index_embeddings.unsqueeze(1)
                .expand(-1, unroll_length, -1, -1)
                .reshape((-1, unroll_length, self.target_dim * self.embed_dim))
            )

            # (batch_size, sub_seq_len, input_dim)
            inputs = torch.cat(
                (input_lags, repeated_index_embeddings, time_feat), dim=-1
            )
        else:
            inputs = torch.cat((input_lags, time_feat), dim=-1)

        # unroll encoder
        outputs, state = self.rnn(inputs, begin_state)

        # assert_shape(outputs, (-1, unroll_length, self.num_cells))
        # for s in state:
        #     assert_shape(s, (-1, self.num_cells))

        # assert_shape(
        #     lags_scaled, (-1, unroll_length, self.target_dim, len(self.lags_seq)),
        # )

        return outputs, state, lags_scaled, inputs

    def unroll_encoder(
        self,
        past_time_feat: torch.Tensor,
        past_target_cdf: torch.Tensor,
        past_observed_values: torch.Tensor,
        past_is_pad: torch.Tensor,
        future_time_feat: Optional[torch.Tensor],
        future_target_cdf: Optional[torch.Tensor],
        target_dimension_indicator: torch.Tensor,
    ) -> Tuple[
        torch.Tensor,
        Union[List[torch.Tensor], torch.Tensor],
        torch.Tensor,
        torch.Tensor,
        torch.Tensor,
    ]:
        """
        Unrolls the RNN encoder over past and, if present, future data.
        Returns outputs and state of the encoder, plus the scale of
        past_target_cdf and a vector of static features that was constructed
        and fed as input to the encoder. All tensor arguments should have NTC
        layout.

        Parameters
        ----------
        past_time_feat
            Past time features (batch_size, history_length, num_features)
        past_target_cdf
            Past marginal CDF transformed target values (batch_size,
            history_length, target_dim)
        past_observed_values
            Indicator whether or not the values were observed (batch_size,
            history_length, target_dim)
        past_is_pad
            Indicator whether the past target values have been padded
            (batch_size, history_length)
        future_time_feat
            Future time features (batch_size, prediction_length, num_features)
        future_target_cdf
            Future marginal CDF transformed target values (batch_size,
            prediction_length, target_dim)
        target_dimension_indicator
            Dimensionality of the time series (batch_size, target_dim)

        Returns
        -------
        outputs
            RNN outputs (batch_size, seq_len, num_cells)
        states
            RNN states. Nested list with (batch_size, num_cells) tensors with
        dimensions target_dim x num_layers x (batch_size, num_cells)
        scale
            Mean scales for the time series (batch_size, 1, target_dim)
        lags_scaled
            Scaled lags(batch_size, sub_seq_len, target_dim, num_lags)
        inputs
            inputs to the RNN

        """

        past_observed_values = torch.min(
            past_observed_values, 1 - past_is_pad.unsqueeze(-1)
        )

        if future_time_feat is None or future_target_cdf is None:
            time_feat = past_time_feat[:, -self.context_length :, ...]
            sequence = past_target_cdf
            sequence_length = self.history_length
            subsequences_length = self.context_length
        else:
            time_feat = torch.cat(
                (past_time_feat[:, -self.context_length :, ...], future_time_feat),
                dim=1,
            )
            sequence = torch.cat((past_target_cdf, future_target_cdf), dim=1)
            sequence_length = self.history_length + self.prediction_length
            subsequences_length = self.context_length + self.prediction_length

        # (batch_size, sub_seq_len, target_dim, num_lags)
        lags = self.get_lagged_subsequences(
            sequence=sequence,
            sequence_length=sequence_length,
            indices=self.lags_seq,
            subsequences_length=subsequences_length,
        )

        # scale is computed on the context length last units of the past target
        # scale shape is (batch_size, 1, target_dim)
        if self.scaler_type == "mean":
            _, scale = self.scaler(
                past_target_cdf[:, -self.context_length :, ...],
                past_observed_values[:, -self.context_length :, ...],
            )
            scale_params = {"scale": scale}
        elif self.scaler_type == "mean_std" or self.scaler_type == "centered_mean":
            _, mean, std = self.scaler(
                past_target_cdf[:, -self.context_length :, ...],
                past_observed_values[:, -self.context_length :, ...],
            )
            scale_params = {"mean": mean, "std": std}
        else:
            raise ValueError(f"Invalid scaler type: {self.scaler_type}")

        # scale is computed on the context length last units of the past target
        # scale shape is (batch_size, 1, target_dim)
        # _, scale = self.scaler(
        #     past_target_cdf[:, -self.context_length :, ...],
        #     past_observed_values[:, -self.context_length :, ...],
        # )

        outputs, states, lags_scaled, inputs = self.unroll(
            lags=lags,
            time_feat=time_feat,
            target_dimension_indicator=target_dimension_indicator,
            unroll_length=subsequences_length,
            begin_state=None,
            scale_params=scale_params,
        )

        return outputs, states, scale_params, lags_scaled, inputs

    def distr_args(self, rnn_outputs: torch.Tensor):
        """
        Returns the distribution of DeepVAR with respect to the RNN outputs.

        Parameters
        ----------
        rnn_outputs
            Outputs of the unrolled RNN (batch_size, seq_len, num_cells)
        scale
            Mean scale for each time series (batch_size, 1, target_dim)

        Returns
        -------
        distr
            Distribution instance
        distr_args
            Distribution arguments
        """
        (distr_args,) = self.proj_dist_args(rnn_outputs)

        return distr_args

    def loss(
        self,
        target_dimension_indicator: torch.Tensor,
        past_target_cdf: torch.Tensor,
        past_observed_values: torch.Tensor,
        past_is_pad: torch.Tensor,
        future_time_feat: torch.Tensor,
        past_time_feat: torch.Tensor,
        future_target_cdf: torch.Tensor,
        future_observed_values: torch.Tensor,
    ) -> Tuple[torch.Tensor, ...]:
        """
        Computes the loss for training DeepVAR, all inputs tensors representing
        time series have NTC layout.

        Parameters
        ----------
        target_dimension_indicator
            Indices of the target dimension (batch_size, target_dim)
        past_time_feat
            Dynamic features of past time series (batch_size, history_length,
            num_features)
        past_target_cdf
            Past marginal CDF transformed target values (batch_size,
            history_length, target_dim)
        past_observed_values
            Indicator whether or not the values were observed (batch_size,
            history_length, target_dim)
        past_is_pad
            Indicator whether the past target values have been padded
            (batch_size, history_length)
        future_time_feat
            Future time features (batch_size, prediction_length, num_features)
        future_target_cdf
            Future marginal CDF transformed target values (batch_size,
            prediction_length, target_dim)
        future_observed_values
            Indicator whether or not the future values were observed
            (batch_size, prediction_length, target_dim)

        Returns
        -------
        distr
            Loss with shape (batch_size, 1)
        likelihoods
            Likelihoods for each time step
            (batch_size, context + prediction_length, 1)
        distr_args
            Distribution arguments (context + prediction_length,
            number_of_arguments)
        """

        seq_len = self.context_length + self.prediction_length

        # unroll the decoder in "training mode", i.e. by providing future data
        # as well
        rnn_outputs, _, scale_params, _, _ = self.unroll_encoder(
            past_time_feat=past_time_feat,
            past_target_cdf=past_target_cdf,
            past_observed_values=past_observed_values,
            past_is_pad=past_is_pad,
            future_time_feat=future_time_feat,
            future_target_cdf=future_target_cdf,
            target_dimension_indicator=target_dimension_indicator,
        )

        # put together target sequence
        # (batch_size, seq_len, target_dim)
        target = torch.cat(
            (past_target_cdf[:, -self.context_length :, ...], future_target_cdf),
            dim=1,
        )

        # assert_shape(target, (-1, seq_len, self.target_dim))

        distr_args = self.distr_args(rnn_outputs=rnn_outputs)

        if self.scaling:
            self.diffusion.scale = scale_params

        # we sum the last axis to have the same shape for all likelihoods
        # (batch_size, subseq_length, 1)

        likelihoods = self.diffusion.log_prob(target, distr_args).unsqueeze(-1)

        # assert_shape(likelihoods, (-1, seq_len, 1))

        past_observed_values = torch.min(
            past_observed_values, 1 - past_is_pad.unsqueeze(-1)
        )

        # (batch_size, subseq_length, target_dim)
        observed_values = torch.cat(
            (
                past_observed_values[:, -self.context_length :, ...],
                future_observed_values,
            ),
            dim=1,
        )

        # mask the loss at one time step if one or more observations is missing
        # in the target dimensions (batch_size, subseq_length, 1)
        loss_weights, _ = observed_values.min(dim=-1, keepdim=True)

        # assert_shape(loss_weights, (-1, seq_len, 1))

        loss = weighted_average(likelihoods, weights=loss_weights, dim=1)

        # assert_shape(loss, (-1, -1, 1))

        # self.distribution = distr

        return (loss.mean(), likelihoods, distr_args)

    def sampling_decoder(
        self,
        past_target_cdf: torch.Tensor,
        target_dimension_indicator: torch.Tensor,
        time_feat: torch.Tensor,
        begin_states: Union[List[torch.Tensor], torch.Tensor],
        scale_params: dict,
    ) -> torch.Tensor:
        """
        Computes sample paths by unrolling the RNN starting with a initial
        input and state.

        Parameters
        ----------
        past_target_cdf
            Past marginal CDF transformed target values (batch_size,
            history_length, target_dim)
        target_dimension_indicator
            Indices of the target dimension (batch_size, target_dim)
        time_feat
            Dynamic features of future time series (batch_size, history_length,
            num_features)
        scale
            Mean scale for each time series (batch_size, 1, target_dim)
        begin_states
            List of initial states for the RNN layers (batch_size, num_cells)
        Returns
        --------
        sample_paths : Tensor
            A tensor containing sampled paths. Shape: (1, num_sample_paths,
            prediction_length, target_dim).
        """

        def repeat(tensor, dim=0):
            return tensor.repeat_interleave(repeats=self.num_parallel_samples, dim=dim)

        def repeat_dict(dict, dim=0):
            return {
                key: value.repeat_interleave(repeats=self.num_parallel_samples, dim=dim)
                for key, value in dict.items()
            }

        # blows-up the dimension of each tensor to
        # batch_size * self.num_sample_paths for increasing parallelism
        repeated_past_target_cdf = repeat(past_target_cdf)
        repeated_time_feat = repeat(time_feat)
        # repeated_scale = repeat(scale)
        if type(scale_params) == dict:
            repeated_scale = repeat_dict(scale_params)
        else:
            repeated_scale = scale_params
        if self.scaling:
            self.diffusion.scale = repeated_scale
        repeated_target_dimension_indicator = repeat(target_dimension_indicator)

        if self.cell_type == "LSTM":
            repeated_states = [repeat(s, dim=1) for s in begin_states]
        else:
            repeated_states = repeat(begin_states, dim=1)

        future_samples = []

        # for each future time-units we draw new samples for this time-unit
        # and update the state
        for k in range(self.prediction_length):
            lags = self.get_lagged_subsequences(
                sequence=repeated_past_target_cdf,
                sequence_length=self.history_length + k,
                indices=self.shifted_lags,
                subsequences_length=1,
            )

            rnn_outputs, repeated_states, _, _ = self.unroll(
                begin_state=repeated_states,
                lags=lags,
                # scale=repeated_scale,
                scale_params=repeated_scale,
                time_feat=repeated_time_feat[:, k : k + 1, ...],
                target_dimension_indicator=repeated_target_dimension_indicator,
                unroll_length=1,
            )

            distr_args = self.distr_args(rnn_outputs=rnn_outputs)

            # (batch_size, 1, target_dim)
            new_samples = self.diffusion.sample(cond=distr_args)

            # (batch_size, seq_len, target_dim)
            future_samples.append(new_samples)
            repeated_past_target_cdf = torch.cat(
                (repeated_past_target_cdf, new_samples), dim=1
            )

        # (batch_size * num_samples, prediction_length, target_dim)
        samples = torch.cat(future_samples, dim=1)

        # (batch_size, num_samples, prediction_length, target_dim)
        return samples.reshape(
            (
                -1,
                self.num_parallel_samples,
                self.prediction_length,
                self.target_dim,
            )
        )

    def forward(
        self,
        target_dimension_indicator: torch.Tensor,
        past_target_cdf,
        past_observed_values,
        past_is_pad,
        future_time_feat,
        past_time_feat,
        future_target_cdf,
        future_observed_values,
    ) -> torch.Tensor:
        """
        Predicts samples given the trained DeepVAR model.
        All tensors should have NTC layout.
        Parameters
        ----------
        target_dimension_indicator
            Indices of the target dimension (batch_size, target_dim)
        past_time_feat
            Dynamic features of past time series (batch_size, history_length,
            num_features)
        past_target_cdf
            Past marginal CDF transformed target values (batch_size,
            history_length, target_dim)
        past_observed_values
            Indicator whether or not the values were observed (batch_size,
            history_length, target_dim)
        past_is_pad
            Indicator whether the past target values have been padded
            (batch_size, history_length)
        future_time_feat
            Future time features (batch_size, prediction_length, num_features)

        Returns
        -------
        sample_paths : Tensor
            A tensor containing sampled paths (1, num_sample_paths,
            prediction_length, target_dim).

        """

        # mark padded data as unobserved
        # (batch_size, target_dim, seq_len)
        past_observed_values = torch.min(
            past_observed_values, 1 - past_is_pad.unsqueeze(-1)
        )

        # unroll the decoder in "prediction mode", i.e. with past data only
        _, begin_states, scale_params, _, _ = self.unroll_encoder(
            past_time_feat=past_time_feat,
            past_target_cdf=past_target_cdf,
            past_observed_values=past_observed_values,
            past_is_pad=past_is_pad,
            future_time_feat=None,
            future_target_cdf=None,
            target_dimension_indicator=target_dimension_indicator,
        )

        return self.sampling_decoder(
            past_target_cdf=past_target_cdf,
            target_dimension_indicator=target_dimension_indicator,
            time_feat=future_time_feat,
            scale_params=scale_params,
            begin_states=begin_states,
        )

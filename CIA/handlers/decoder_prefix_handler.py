from CIA.handlers.handler import Handler
from CIA.dataloaders.dataloader import DataloaderGenerator
from CIA.utils import all_reduce_scalar, is_main_process, to_numpy, \
    top_k_top_p_filtering
import torch
from tqdm import tqdm
from itertools import islice
import numpy as np
from torch.nn.parallel import DistributedDataParallel
import torch.distributed as dist


class DecoderPrefixHandler(Handler):
    def __init__(self, model: DistributedDataParallel, model_dir: str,
                 dataloader_generator: DataloaderGenerator) -> None:
        super().__init__(model=model,
                         model_dir=model_dir,
                         dataloader_generator=dataloader_generator)

    def plot(self, epoch_id, monitored_quantities_train,
             monitored_quantities_val) -> None:
        if is_main_process():
            for k, v in monitored_quantities_train.items():
                self.writer.add_scalar(f'{k}/train', v, epoch_id)
            for k, v in monitored_quantities_val.items():
                self.writer.add_scalar(f'{k}/val', v, epoch_id)

    def forward_with_states(self,
                            target,
                            metadata_dict,
                            h_pe_init=None):
        return self.model.module.forward_with_states(
            target, metadata_dict=metadata_dict, h_pe_init=h_pe_init)

    def load(self, early_stopped, recurrent):
        map_location = {'cuda:0': f'cuda:{dist.get_rank()}'}
        print(f'Loading models {self.__repr__()}')
        if early_stopped:
            print('Load early stopped model')
            model_dir = f'{self.model_dir}/early_stopped'
        else:
            print('Load over-fitted model')
            model_dir = f'{self.model_dir}/overfitted'

        state_dict = torch.load(f'{model_dir}/model',
                                map_location=map_location)

        # if recurrent, we must also load the "with_states" version
        if recurrent:
            transformer_with_states_dict = {}
            for k, v in state_dict.items():
                if 'transformer' in k:
                    new_key = k.replace(
                        'transformer.transformer', 'transformer.transformer_with_states')
                    transformer_with_states_dict[new_key] = v
            state_dict.update(transformer_with_states_dict)
        self.model.load_state_dict(
            state_dict=state_dict
        )

    # ==== Training methods

    def epoch(
        self,
        data_loader,
        train=True,
        num_batches=None,
    ):
        means = None

        if train:
            self.train()
        else:
            self.eval()

        h_pe_init = None

        iterator = enumerate(islice(data_loader, num_batches))
        if is_main_process():
            iterator = tqdm(iterator, ncols=80)

        for sample_id, tensor_dict in iterator:

            # ==========================
            with torch.no_grad():
                x = tensor_dict['x']
                x, metadata_dict = self.data_processor.preprocess(x)

            # ========Train decoder =============
            self.optimizer.zero_grad()
            forward_pass = self.forward(target=x,
                                        metadata_dict=metadata_dict,
                                        h_pe_init=h_pe_init)
            loss = forward_pass['loss']
            # h_pe_init = forward_pass['h_pe'].detach()

            if train:
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.parameters(), 5)
                self.optimizer.step()

            # Monitored quantities
            monitored_quantities = forward_pass['monitored_quantities']

            # average quantities
            if means is None:
                means = {key: 0 for key in monitored_quantities}
            means = {
                key: value + means[key]
                for key, value in monitored_quantities.items()
            }

            del loss

        # renormalize monitored quantities
        for key, value in means.items():
            means[key] = all_reduce_scalar(value,
                                           average=True) / (sample_id + 1)

        # means = {
        #     key: all_reduce_scalar(value, average=True) / (sample_id + 1)
        #     for key, value in means.items()
        # }
        return means

    def inpaint_non_optimized(self, x, metadata_dict, temperature=1., top_p=1., top_k=0):
        # TODO add arguments to preprocess
        print(f'Placeholder duration: {metadata_dict["placeholder_duration"]}')
        self.eval()
        batch_size, num_events, _ = x.size()

        # TODO only works with batch_size=1 at present
        assert x.size(0) == 1

        decoding_end = None
        decoding_start_event = metadata_dict['decoding_start']
        x[:, decoding_start_event:] = 0
        with torch.no_grad():
            # i corresponds to the position of the token BEING generated
            for event_index in range(decoding_start_event, num_events):
                for channel_index in range(self.num_channels_target):
                    i = event_index * self.num_channels_target + channel_index

                    forward_pass = self.forward_step(
                        target=x,
                        metadata_dict=metadata_dict,
                        state=None,
                        i=i,
                        h_pe=None)
                    weights = forward_pass['weights']

                    logits = weights / temperature

                    filtered_logits = []
                    for logit in logits:
                        filter_logit = top_k_top_p_filtering(logit,
                                                             top_k=top_k,
                                                             top_p=top_p)
                        filtered_logits.append(filter_logit)
                    filtered_logits = torch.stack(filtered_logits, dim=0)
                    # Sample from the filtered distribution
                    p = to_numpy(torch.softmax(filtered_logits, dim=-1))

                    # update generated sequence
                    for batch_index in range(batch_size):
                        if event_index >= decoding_start_event:
                            new_pitch_index = np.random.choice(np.arange(
                                self.num_tokens_per_channel_target[channel_index]),
                                p=p[batch_index])
                            x[batch_index, event_index, channel_index] = int(
                                new_pitch_index)

                            end_symbol_index = self.dataloader_generator.dataset.value2index[
                                self.dataloader_generator.features[channel_index]]['END']
                            if end_symbol_index == int(new_pitch_index):
                                decoding_end = event_index

                if decoding_end is not None:
                    break
        if decoding_end is None:
            done = False
            decoding_end = num_events
        else:
            done = True

        print(f'Num events_generated: {decoding_end - decoding_start_event}')
        # to score
        x_inpainted = self.data_processor.postprocess(
            x.cpu(),
            decoding_end,
            metadata_dict
        )
        generated_region = x[:, decoding_start_event:decoding_end]

        return x_inpainted, generated_region, done

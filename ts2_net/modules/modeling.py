from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging

import torch
import torch.nn.functional as F
from torch import nn

from modules.until_module import PreTrainedModel, AllGather, CrossEn, BTloss, ClassifyCrossEn, TextPromptEncoder, VideoPromptEncoder, make_patch_shift, make_attn_visual, make_token_shuffle
from modules.module_cross import CrossModel, CrossConfig, Transformer as TransformerClip
from modules.differential_topk import VisualTokenSelection,VisualTokenSelection_,VisualTokenSelection_1,VisualTokenSelection_2, TextTokenSelection, VisualTokenRandomSelection, STVisualTokenSelection

from modules.module_clip import CLIP, convert_weights
from torch.nn.utils.rnn import pad_packed_sequence, pack_padded_sequence

logger = logging.getLogger(__name__)
allgather = AllGather.apply

class CLIP4ClipPreTrainedModel(PreTrainedModel, nn.Module):
    """ An abstract class to handle weights initialization and
        a simple interface for dowloading and loading pretrained models.
    """
    def __init__(self, cross_config, *inputs, **kwargs):
        super(CLIP4ClipPreTrainedModel, self).__init__(cross_config)
        self.cross_config = cross_config
        self.clip = None
        self.cross = None

    @classmethod
    def from_pretrained(cls, cross_model_name, state_dict=None, cache_dir=None, type_vocab_size=2, *inputs, **kwargs):

        task_config = None
        if "task_config" in kwargs.keys():
            task_config = kwargs["task_config"]
            if not hasattr(task_config, "local_rank"):
                task_config.__dict__["local_rank"] = 0
            elif task_config.local_rank == -1:
                task_config.local_rank = 0

        if state_dict is None: state_dict = {}
        pretrained_clip_name = "ViT-B/32"
        if hasattr(task_config, 'pretrained_clip_name'):
            pretrained_clip_name = task_config.pretrained_clip_name
        clip_state_dict = CLIP.get_config(pretrained_clip_name=pretrained_clip_name)
        for key, val in clip_state_dict.items():
            new_key = "clip." + key
            if new_key not in state_dict:
                state_dict[new_key] = val.clone()

            if 'clip.visual' in new_key:
                slicing = new_key.replace("visual","visual_Coarse")
                state_dict[slicing] = val.clone()
                slicing = new_key.replace("visual","visual_multi")
                state_dict[slicing] = val.clone()
                slicing = new_key.replace("visual","visual_Coarse_multi")
                state_dict[slicing] = val.clone()
            elif 'clip.transformer' in new_key:
                slicing = new_key.replace("transformer","transformer_Coarse")
                state_dict[slicing] = val.clone()
                slicing = new_key.replace("transformer","transformer_multi")
                state_dict[slicing] = val.clone()
                slicing = new_key.replace("transformer","transformer_Coarse_multi")
                state_dict[slicing] = val.clone()
            elif 'clip.text_projection' in new_key:
                slicing = new_key.replace("text_projection","text_projection_")
                state_dict[slicing] = val.clone()
            elif 'clip.token_embedding' in new_key:
                slicing = new_key.replace("token_embedding","token_embedding_")
                state_dict[slicing] = val.clone()
            elif 'clip.positional_embedding' in new_key:
                slicing = new_key.replace("positional_embedding","positional_embedding_")
                state_dict[slicing] = val.clone()
            elif 'clip.ln_final' in new_key:
                slicing = new_key.replace("ln_final","ln_final_")
                state_dict[slicing] = val.clone()

        cross_config, _ = CrossConfig.get_config(cross_model_name, cache_dir, type_vocab_size, state_dict=None, task_config=task_config)

        model = cls(cross_config, clip_state_dict, *inputs, **kwargs)

        ## ===> Initialization trick [HARD CODE]
        if model.linear_patch == "3d":
            contain_conv2 = False
            for key in state_dict.keys():
                if key.find("visual.conv2.weight") > -1:
                    contain_conv2 = True
                    break
            if contain_conv2 is False and hasattr(model.clip.visual, "conv2"):
                cp_weight = state_dict["clip.visual.conv1.weight"].clone()
                kernel_size = model.clip.visual.conv2.weight.size(2)
                conv2_size = model.clip.visual.conv2.weight.size()
                conv2_size = list(conv2_size)

                left_conv2_size = conv2_size.copy()
                right_conv2_size = conv2_size.copy()
                left_conv2_size[2] = (kernel_size - 1) // 2
                right_conv2_size[2] = kernel_size - 1 - left_conv2_size[2]

                left_zeros, right_zeros = None, None
                if left_conv2_size[2] > 0:
                    left_zeros = torch.zeros(*tuple(left_conv2_size), dtype=cp_weight.dtype, device=cp_weight.device)
                if right_conv2_size[2] > 0:
                    right_zeros = torch.zeros(*tuple(right_conv2_size), dtype=cp_weight.dtype, device=cp_weight.device)

                cat_list = []
                if left_zeros != None: cat_list.append(left_zeros)
                cat_list.append(cp_weight.unsqueeze(2))
                if right_zeros != None: cat_list.append(right_zeros)
                cp_weight = torch.cat(cat_list, dim=2)

                state_dict["clip.visual.conv2.weight"] = cp_weight

        if model.sim_header == 'tightTransf':
            contain_cross = False
            for key in state_dict.keys():
                if key.find("cross.transformer") > -1:
                    contain_cross = True
                    break
            if contain_cross is False:
                for key, val in clip_state_dict.items():
                    if key == "positional_embedding":
                        state_dict["cross.embeddings.position_embeddings.weight"] = val.clone()
                        continue
                    if key.find("transformer.resblocks") == 0:
                        num_layer = int(key.split(".")[2])

                        # cut from beginning
                        if num_layer < task_config.cross_num_hidden_layers:
                            state_dict["cross."+key] = val.clone()
                            continue

        if model.sim_header == "seqLSTM" or model.sim_header == "seqTransf":
            # This step is to detect whether in train mode or test mode
            contain_frame_position = False
            for key in state_dict.keys():
                if key.find("frame_position_embeddings") > -1:
                    contain_frame_position = True
                    break

            # train mode
            if contain_frame_position is False:
                for key, val in clip_state_dict.items():
                    if key == "positional_embedding":
                        state_dict["frame_position_embeddings.weight"] = val.clone()
                        state_dict["frame_position_embeddings_.weight"] = val.clone()
                        # state_dict["text_prompt_encoder.pos_embedding"] = val[0:3].clone()
                        continue
                    if model.sim_header == "seqTransf" and key.find("transformer.resblocks") == 0:
                        num_layer = int(key.split(".")[2])
                        # cut from beginning
                        if num_layer < task_config.cross_num_hidden_layers:
                            state_dict[key.replace("transformer.", "transformerClip.")] = val.clone()
                            continue
            # test mode
            else:
                for key, val in state_dict.items():
                    # test mode
                    if  key.find("clip.visual.transformer.resblocks") == 0:
                            num_layer = int(key.split(".")[4])
                            # shift layers 10-11
                            if num_layer >=10 and num_layer < 12:
                                state_dict[key.replace("attn.net.", "attn.")] = val.clone()
        ## <=== End of initialization trick

        if state_dict is not None:
            model = cls.init_preweight(model, state_dict, task_config=task_config)

        make_patch_shift(model, video_frame=task_config.max_frames, n_div=7)
        return model

def show_log(task_config, info):
    if task_config is None or task_config.local_rank == 0:
        logger.warning(info)

def update_attr(target_name, target_config, target_attr_name, source_config, source_attr_name, default_value=None):
    if hasattr(source_config, source_attr_name):
        if default_value is None or getattr(source_config, source_attr_name) != default_value:
            setattr(target_config, target_attr_name, getattr(source_config, source_attr_name))
            show_log(source_config, "Set {}.{}: {}.".format(target_name,
                                                            target_attr_name, getattr(target_config, target_attr_name)))
    return target_config

def check_attr(target_name, task_config):
    return hasattr(task_config, target_name) and task_config.__dict__[target_name]

class CLIP4Clip(CLIP4ClipPreTrainedModel):
    def __init__(self, cross_config, clip_state_dict, task_config):
        super(CLIP4Clip, self).__init__(cross_config)
        self.task_config = task_config
        self.ignore_video_index = -1

        assert self.task_config.max_words + self.task_config.max_frames <= cross_config.max_position_embeddings

        self._stage_one = True
        self._stage_two = False

        show_log(task_config, "Stage-One:{}, Stage-Two:{}".format(self._stage_one, self._stage_two))

        self.loose_type = False
        if self._stage_one and check_attr('loose_type', self.task_config):
            self.loose_type = True
            show_log(task_config, "Test retrieval by loose type.")

        # CLIP Encoders: From OpenAI: CLIP [https://github.com/openai/CLIP] ===>
        vit = "visual.proj" in clip_state_dict
        assert vit
        if vit:
            vision_width = clip_state_dict["visual.conv1.weight"].shape[0]
            vision_layers = len(
                [k for k in clip_state_dict.keys() if k.startswith("visual.") and k.endswith(".attn.in_proj_weight")])
            vision_patch_size = clip_state_dict["visual.conv1.weight"].shape[-1]
            grid_size = round((clip_state_dict["visual.positional_embedding"].shape[0] - 1) ** 0.5)
            image_resolution = vision_patch_size * grid_size
        else:
            counts: list = [len(set(k.split(".")[2] for k in clip_state_dict if k.startswith(f"visual.layer{b}"))) for b in
                            [1, 2, 3, 4]]
            vision_layers = tuple(counts)
            vision_width = clip_state_dict["visual.layer1.0.conv1.weight"].shape[0]
            output_width = round((clip_state_dict["visual.attnpool.positional_embedding"].shape[0] - 1) ** 0.5)
            vision_patch_size = None
            assert output_width ** 2 + 1 == clip_state_dict["visual.attnpool.positional_embedding"].shape[0]
            image_resolution = output_width * 32

        embed_dim = clip_state_dict["text_projection"].shape[1]
        context_length = clip_state_dict["positional_embedding"].shape[0]
        vocab_size = clip_state_dict["token_embedding.weight"].shape[0]
        transformer_width = clip_state_dict["ln_final.weight"].shape[0]
        transformer_heads = transformer_width // 64
        transformer_layers = len(set(k.split(".")[2] for k in clip_state_dict if k.startswith(f"transformer.resblocks")))

        show_log(task_config, "\t embed_dim: {}".format(embed_dim))
        show_log(task_config, "\t image_resolution: {}".format(image_resolution))
        show_log(task_config, "\t vision_layers: {}".format(vision_layers))
        show_log(task_config, "\t vision_width: {}".format(vision_width))
        show_log(task_config, "\t vision_patch_size: {}".format(vision_patch_size))
        show_log(task_config, "\t context_length: {}".format(context_length))
        show_log(task_config, "\t vocab_size: {}".format(vocab_size))
        show_log(task_config, "\t transformer_width: {}".format(transformer_width))
        show_log(task_config, "\t transformer_heads: {}".format(transformer_heads))
        show_log(task_config, "\t transformer_layers: {}".format(transformer_layers))

        self.linear_patch = '2d'
        if hasattr(task_config, "linear_patch"):
            self.linear_patch = task_config.linear_patch
            show_log(task_config, "\t\t linear_patch: {}".format(self.linear_patch))

        # use .float() to avoid overflow/underflow from fp16 weight. https://github.com/openai/CLIP/issues/40
        cut_top_layer = 0
        show_log(task_config, "\t cut_top_layer: {}".format(cut_top_layer))
        self.clip = CLIP(
            embed_dim,
            image_resolution, vision_layers-cut_top_layer, vision_width, vision_patch_size,
            context_length, vocab_size, transformer_width, transformer_heads, transformer_layers-cut_top_layer,
            linear_patch=self.linear_patch
        ).float()

        for key in ["input_resolution", "context_length", "vocab_size"]:
            if key in clip_state_dict:
                del clip_state_dict[key]

        convert_weights(self.clip)
        # <=== End of CLIP Encoders

        self.sim_header = 'meanP'
        if hasattr(task_config, "sim_header"):
            self.sim_header = task_config.sim_header
            show_log(task_config, "\t sim_header: {}".format(self.sim_header))
        if self.sim_header == "tightTransf": assert self.loose_type is False

        cross_config.max_position_embeddings = context_length
        if self.loose_type is False:
            # Cross Encoder ===>
            cross_config = update_attr("cross_config", cross_config, "num_hidden_layers", self.task_config, "cross_num_hidden_layers")
            self.cross = CrossModel(cross_config)
            # <=== End of Cross Encoder
            self.similarity_dense = nn.Linear(cross_config.hidden_size, 1)

        if self.sim_header == "seqLSTM" or self.sim_header == "seqTransf":
            self.frame_position_embeddings = nn.Embedding(cross_config.max_position_embeddings, cross_config.hidden_size)
            self.frame_position_embeddings_ = nn.Embedding(cross_config.max_position_embeddings, cross_config.hidden_size)
            # self.frame_position_embeddings = nn.Embedding(600, cross_config.hidden_size)
        if self.sim_header == "seqTransf":
            self.transformerClip = TransformerClip(width=transformer_width, layers=self.task_config.cross_num_hidden_layers,
                                                   heads=transformer_heads, )
        if self.sim_header == "seqLSTM":
            self.lstm_visual = nn.LSTM(input_size=cross_config.hidden_size, hidden_size=cross_config.hidden_size,
                                       batch_first=True, bidirectional=False, num_layers=1)

        self.loss_fct = CrossEn()
        self.frame_match_weight = 1.0
        self.visual_prompt_len = 4
        self.visual_prompt_encoder = VideoPromptEncoder(self.visual_prompt_len, vision_width, vision_patch_size)

        # Notation: In practice, we manually regard [CLS] token as the most informative, 
        # which means we only select other K-1 tokens in selection module
        self.visual_token_selector = VisualTokenSelection(self.task_config.max_frames, embed_dim, topk=3)
        self.visual_token_selector_ = VisualTokenSelection_(self.task_config.max_frames, embed_dim, topk=3)
        self.visual_token_selector_1 = VisualTokenSelection_1(self.task_config.max_frames, embed_dim, topk=3)
        self.visual_token_selector_2 = VisualTokenSelection_2(self.task_config.max_frames, embed_dim, topk=3)
        self.text_token_selector = TextTokenSelection(embed_dim, topk=1)

        self.apply(self.init_weights)

    def forward(self, input_ids, token_type_ids, attention_mask, video, video_mask=None):
        input_ids = input_ids.view(-1, input_ids.shape[-1])
        token_type_ids = token_type_ids.view(-1, token_type_ids.shape[-1])
        attention_mask = attention_mask.view(-1, attention_mask.shape[-1])
        video_mask = video_mask.view(-1, video_mask.shape[-1])
        # print('video mask shape in forward: ', video_mask.shape)

        # T x 3 x H x W
        video = torch.as_tensor(video).float()
        b, pair, bs, ts, channel, h, w = video.shape
        video = video.view(b * pair * bs * ts, channel, h, w)
        video_frame = bs * ts

        sequence_hidden,sequence_hidden_coarse, visual_hidden,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse = self.get_sequence_visual_output(input_ids, token_type_ids, attention_mask,
                                                                         video, video_mask, shaped=True, video_frame=video_frame)

        if self.training:
            loss = 0.
            ### TODO: need to simplify the code to calculate similarity ####
            sim_matrix_semantic, sim_matrix_global = self.get_similarity_logits(sequence_hidden,sequence_hidden_coarse, visual_hidden,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse, attention_mask, video_mask,
                                                    shaped=True, loose_type=self.loose_type)
            # text2video
            sim_loss1 = self.loss_fct(sim_matrix_semantic)
            # video2text
            sim_loss2 = self.loss_fct(sim_matrix_semantic.T)
            sim_loss_semantic = (sim_loss1 + sim_loss2) / 2
            loss = loss + self.frame_match_weight*sim_loss_semantic

            # text2video
            sim_loss1 = self.loss_fct(sim_matrix_global)
            # video2text
            sim_loss2 = self.loss_fct(sim_matrix_global.T)
            sim_loss_global = (sim_loss1 + sim_loss2) / 2
            loss = loss + (1-self.frame_match_weight)*sim_loss_global

            return loss
        else:
            return None

    def get_sequence_output(self, input_ids, token_type_ids, attention_mask, shaped=False):
        if shaped is False:
            input_ids = input_ids.view(-1, input_ids.shape[-1])
            token_type_ids = token_type_ids.view(-1, token_type_ids.shape[-1])
            attention_mask = attention_mask.view(-1, attention_mask.shape[-1])

        bs_pair = input_ids.size(0)
        sequence_hidden,sequence_hidden_coarse = self.clip.encode_text(input_ids)
        sequence_hidden,sequence_hidden_coarse = sequence_hidden.float(),sequence_hidden_coarse.float()
        
        sequence_hidden = sequence_hidden.view(bs_pair, -1, sequence_hidden.size(-1))
        sequence_hidden_coarse = sequence_hidden_coarse.view(bs_pair, -1, sequence_hidden_coarse.size(-1))
        # sequence_hidden = self.text_token_selector(sequence_hidden, input_ids, attention_mask)

        return sequence_hidden,sequence_hidden_coarse

    def get_visual_output(self, video, video_mask, shaped=False, video_frame=-1):
        if shaped is False:
            video_mask = video_mask.view(-1, video_mask.shape[-1])
            video = torch.as_tensor(video).float()
            b, pair, bs, ts, channel, h, w = video.shape
            video = video.view(b * pair * bs * ts, channel, h, w)
            video_frame = bs * ts
            
        bs_pair = video_mask.size(0)
        visual_hidden,visual_hidden_Coarse,visual_multi_hidden, visual_multi_hidden_Coarse = self.clip.encode_image(video, video_frame=video_frame)
        visual_hidden,visual_hidden_Coarse,visual_multi_hidden, visual_multi_hidden_Coarse = visual_hidden.float(),visual_hidden_Coarse.float(),visual_multi_hidden.float(), visual_multi_hidden_Coarse.float()
        # print('visual_hidden in get_visual_output before: ', visual_hidden.shape)
        visual_hidden = visual_hidden.view(bs_pair, -1, visual_hidden.size(-1)) # shape here should be (bs, max_frames*sample_len, hid_dim)
        visual_multi_hidden = visual_multi_hidden.view(bs_pair, -1, visual_multi_hidden.size(-1))
        visual_multi_hidden_Coarse = visual_multi_hidden_Coarse.view(bs_pair, -1, visual_multi_hidden_Coarse.size(-1))
        visual_hidden_Coarse = visual_hidden_Coarse.view(bs_pair, -1, visual_hidden_Coarse.size(-1))

        # print('visual_hidden in get_visual_output after: ', visual_hidden.shape)
        visual_hidden = self.visual_token_selector(visual_hidden)
        visual_multi_hidden = self.visual_token_selector_1(visual_multi_hidden)
        visual_multi_hidden_Coarse = self.visual_token_selector_2(visual_multi_hidden_Coarse)
        visual_hidden_Coarse = self.visual_token_selector_(visual_hidden_Coarse)
        return visual_hidden,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse

    def get_sequence_visual_output(self, input_ids, token_type_ids, attention_mask, video, video_mask, shaped=False, video_frame=-1):
        if shaped is False:
            input_ids = input_ids.view(-1, input_ids.shape[-1])
            token_type_ids = token_type_ids.view(-1, token_type_ids.shape[-1])
            attention_mask = attention_mask.view(-1, attention_mask.shape[-1])
            video_mask = video_mask.view(-1, video_mask.shape[-1])

            video = torch.as_tensor(video).float()
            b, pair, bs, ts, channel, h, w = video.shape
            video = video.view(b * pair * bs * ts, channel, h, w)
            video_frame = bs * ts

        sequence_hidden,sequence_hidden_coarse = self.get_sequence_output(input_ids, token_type_ids, attention_mask, shaped=True)
        visual_hidden,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse = self.get_visual_output(video, video_mask, shaped=True, video_frame=video_frame)

        return sequence_hidden,sequence_hidden_coarse, visual_hidden,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse

    def _get_cross_output(self, sequence_output, visual_output, attention_mask, video_mask):

        concat_features = torch.cat((sequence_output, visual_output), dim=1)  # concatnate tokens and frames
        concat_mask = torch.cat((attention_mask, video_mask), dim=1)
        text_type_ = torch.zeros_like(attention_mask)
        video_type_ = torch.ones_like(video_mask)
        concat_type = torch.cat((text_type_, video_type_), dim=1)

        cross_layers, pooled_output = self.cross(concat_features, concat_type, concat_mask, output_all_encoded_layers=True)
        cross_output = cross_layers[-1]

        return cross_output, pooled_output, concat_mask

    def _mean_pooling_for_similarity_sequence(self, sequence_output, attention_mask):
        attention_mask_un = attention_mask.to(dtype=torch.float).unsqueeze(-1)
        attention_mask_un[:, 0, :] = 0.
        sequence_output = sequence_output * attention_mask_un
        text_out = torch.sum(sequence_output, dim=1) / torch.sum(attention_mask_un, dim=1, dtype=torch.float)
        return text_out

    def _mean_pooling_for_similarity_visual(self, visual_output, video_mask,):
        video_mask_un = video_mask.to(dtype=torch.float).unsqueeze(-1)
        visual_output = visual_output * video_mask_un
        video_mask_un_sum = torch.sum(video_mask_un, dim=1, dtype=torch.float)
        video_mask_un_sum[video_mask_un_sum == 0.] = 1.
        video_out = torch.sum(visual_output, dim=1) / video_mask_un_sum
        return video_out

    def _mean_pooling_for_similarity(self, sequence_output, visual_output, attention_mask, video_mask,):
        text_out = self._mean_pooling_for_similarity_sequence(sequence_output, attention_mask)
        video_out = self._mean_pooling_for_similarity_visual(visual_output, video_mask)

        return text_out, video_out

    def _loose_similarity(self, sequence_output,sequence_hidden_coarse, visual_output,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse, attention_mask, video_mask, sim_header="meanP"):
        sequence_output, visual_output = sequence_output.contiguous(), visual_output.contiguous()
        sequence_hidden_coarse = sequence_hidden_coarse.contiguous()
        visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse = visual_hidden_Coarse.contiguous(),visual_multi_hidden.contiguous(),visual_multi_hidden_Coarse.contiguous()
        # print("visual_output.shape: ", visual_output.shape)
        # print("video_mask.shape: ", video_mask.shape)

        loss = 0.

        #########################################################
        # video mask need to change due to more tokens in frame #
        #########################################################
        expand_times = visual_output.shape[1] // video_mask.shape[1]
        # video_mask shape here is (bs, max_frames)
        video_mask = video_mask.unsqueeze(1).repeat(1,1,expand_times).view(video_mask.shape[0], -1)
        #########################################################
        # video mask need to change due to more tokens in frame #
        #########################################################

        if sim_header == "meanP":
            # Default: Parameter-free type
            pass
        elif sim_header == "seqLSTM":
            # Sequential type: LSTM
            visual_output_original = visual_output
            visual_output = pack_padded_sequence(visual_output, torch.sum(video_mask, dim=-1).cpu(),
                                                 batch_first=True, enforce_sorted=False)
            visual_output, _ = self.lstm_visual(visual_output)
            if self.training: self.lstm_visual.flatten_parameters()
            visual_output, _ = pad_packed_sequence(visual_output, batch_first=True)
            visual_output = torch.cat((visual_output, visual_output_original[:, visual_output.size(1):, ...].contiguous()), dim=1)
            visual_output = visual_output + visual_output_original
        elif sim_header == "seqTransf":
            # Sequential type: Transformer Encoder
            visual_output_original = visual_output
            visual_hidden_Coarse_original = visual_hidden_Coarse
            visual_multi_hidden_original = visual_multi_hidden
            visual_multi_hidden_Coarse_original = visual_multi_hidden_Coarse
            
            seq_length = visual_output.size(1)
            position_ids = torch.arange(seq_length, dtype=torch.long, device=visual_output.device)
            position_ids = position_ids.unsqueeze(0).expand(visual_output.size(0), -1)
            frame_position_embeddings = self.frame_position_embeddings(position_ids)
            visual_output = visual_output + frame_position_embeddings

            extended_video_mask = (1.0 - video_mask.unsqueeze(1)) * -1000000.0
            extended_video_mask = extended_video_mask.expand(-1, video_mask.size(1), -1)
            visual_output = visual_output.permute(1, 0, 2)  # NLD -> LND
            visual_output = self.transformerClip(visual_output, extended_video_mask)
            visual_output = visual_output.permute(1, 0, 2)  # LND -> NLD
            # consider remove below statement because it seems non-sense......(cannot, performance decay)
            visual_output = visual_output + visual_output_original
        
            position_ids = torch.arange(seq_length, dtype=torch.long, device=visual_output.device)
            position_ids = position_ids.unsqueeze(0).expand(visual_hidden_Coarse.size(0), -1)
            frame_position_embeddings = self.frame_position_embeddings(position_ids)
            visual_hidden_Coarse = visual_hidden_Coarse + frame_position_embeddings

            extended_video_mask = (1.0 - video_mask.unsqueeze(1)) * -1000000.0
            extended_video_mask = extended_video_mask.expand(-1, video_mask.size(1), -1)
            visual_hidden_Coarse = visual_hidden_Coarse.permute(1, 0, 2)  # NLD -> LND
            visual_hidden_Coarse = self.transformerClip(visual_hidden_Coarse, extended_video_mask)
            visual_hidden_Coarse = visual_hidden_Coarse.permute(1, 0, 2)  # LND -> NLD
            visual_hidden_Coarse = visual_hidden_Coarse + visual_hidden_Coarse_original

            position_ids = torch.arange(seq_length, dtype=torch.long, device=visual_output.device)
            position_ids = position_ids.unsqueeze(0).expand(visual_multi_hidden.size(0), -1)
            frame_position_embeddings = self.frame_position_embeddings(position_ids)
            visual_multi = visual_multi_hidden + frame_position_embeddings

            extended_video_mask = (1.0 - video_mask.unsqueeze(1)) * -1000000.0
            extended_video_mask = extended_video_mask.expand(-1, video_mask.size(1), -1)
            visual_multi = visual_multi.permute(1, 0, 2)  # NLD -> LND
            visual_multi = self.transformerClip(visual_multi, extended_video_mask)
            visual_multi = visual_multi.permute(1, 0, 2)  # LND -> NLD
            visual_multi = visual_multi + visual_multi_hidden_original

            position_ids = torch.arange(seq_length, dtype=torch.long, device=visual_output.device)
            position_ids = position_ids.unsqueeze(0).expand(visual_multi_hidden_Coarse.size(0), -1)
            frame_position_embeddings = self.frame_position_embeddings(position_ids)
            visual_multi_coarse = visual_multi_hidden_Coarse + frame_position_embeddings

            extended_video_mask = (1.0 - video_mask.unsqueeze(1)) * -1000000.0
            extended_video_mask = extended_video_mask.expand(-1, video_mask.size(1), -1)
            visual_multi_coarse = visual_multi_coarse.permute(1, 0, 2)  # NLD -> LND
            visual_multi_coarse = self.transformerClip(visual_multi_coarse, extended_video_mask)
            visual_multi_coarse = visual_multi_coarse.permute(1, 0, 2)  # LND -> NLD
            visual_multi_coarse = visual_multi_coarse + visual_multi_hidden_Coarse_original

        
        #########################################################
        # video mask need to change due to more tokens in frame #
        #########################################################
        frame_embedding_index = torch.arange(start=0, end=visual_output.shape[1], step=expand_times, dtype=torch.long,
                                                        device=visual_output.device)
        visual_output = visual_output[:, frame_embedding_index, :]
        visual_hidden_Coarse = visual_hidden_Coarse[:, frame_embedding_index, :]
        visual_multi = visual_multi[:, frame_embedding_index, :]
        visual_multi_coarse = visual_multi_coarse[:, frame_embedding_index, :]
        visual_output_original = visual_output_original[:, frame_embedding_index, :]
        visual_hidden_Coarse_original = visual_hidden_Coarse_original[:, frame_embedding_index, :]
        visual_multi_hidden_original = visual_multi_hidden_original[:, frame_embedding_index, :]
        visual_multi_hidden_Coarse_original = visual_multi_hidden_Coarse_original[:, frame_embedding_index, :]
        video_mask = video_mask[:, frame_embedding_index]
        #########################################################
        # video mask need to change due to more tokens in frame #
        #########################################################
        
        if self.training:
            visual_output = allgather(visual_output, self.task_config)
            visual_hidden_Coarse = allgather(visual_hidden_Coarse, self.task_config)
            visual_multi = allgather(visual_multi, self.task_config)
            visual_multi_coarse = allgather(visual_multi_coarse, self.task_config)            
            # visual_output_original = allgather(visual_output_original, self.task_config)
            video_mask = allgather(video_mask, self.task_config)
            sequence_output = allgather(sequence_output, self.task_config)
            sequence_hidden_coarse = allgather(sequence_hidden_coarse, self.task_config)
            attention_mask = allgather(attention_mask, self.task_config)
            torch.distributed.barrier()

        sequence_output_dummy = sequence_output.clone().detach()
        sequence_output_coarse_dummy = sequence_hidden_coarse.clone().detach()
        # get frame sim
        sim_matrix_semantic = self.get_frame_selectedcls_similarity(sequence_output, visual_output, attention_mask, video_mask)
        # sim_matrix_semantic = self.get_global_similarity(sequence_output, visual_output, attention_mask, video_mask)
        sim_matrix_semantic_ = self.get_frame_selectedcls_similarity(sequence_hidden_coarse, visual_hidden_Coarse, attention_mask, video_mask)
        sim_matrix_semantic_multi = self.get_frame_selectedcls_similarity(sequence_output_coarse_dummy, visual_multi, attention_mask, video_mask)
        sim_matrix_semantic_multi_coarse = self.get_frame_selectedcls_similarity(sequence_output_dummy, visual_multi_coarse, attention_mask, video_mask)

        # get global sim
        sim_matrix_global = self.get_frame_similarity(sequence_output, visual_output, attention_mask, video_mask)
        sim_matrix_global_ = self.get_frame_similarity(sequence_hidden_coarse, visual_hidden_Coarse, attention_mask, video_mask)
        sim_matrix_global_multi = self.get_frame_similarity(sequence_output_coarse_dummy, visual_multi, attention_mask, video_mask)
        sim_matrix_global_multi_coarse = self.get_frame_similarity(sequence_output_dummy, visual_multi_coarse, attention_mask, video_mask)
        
        logit_sim_matrix_semantic_multi = (sim_matrix_semantic_multi + sim_matrix_semantic_multi_coarse)/2
        sim_matrix_semantic = (sim_matrix_semantic * 0.35 + sim_matrix_semantic_ * 0.35 + logit_sim_matrix_semantic_multi * 0.3)

        logit_sim_matrix_global_multi = (sim_matrix_global_multi + sim_matrix_global_multi_coarse)/2
        sim_matrix_global = (sim_matrix_global * 0.35 + sim_matrix_global_ * 0.35 + logit_sim_matrix_global_multi * 0.3)

        return sim_matrix_global, sim_matrix_semantic
        # return retrieve_logits

    def get_global_similarity(self, sequence_output, visual_output, attention_mask, video_mask):
        visual_output = visual_output / visual_output.norm(dim=-1, keepdim=True)
        visual_output = self._mean_pooling_for_similarity_visual(visual_output, video_mask)
        visual_output = visual_output / visual_output.norm(dim=-1, keepdim=True)

        sequence_output = sequence_output.squeeze(1)
        sequence_output = sequence_output / sequence_output.norm(dim=-1, keepdim=True)

        logit_scale = self.clip.logit_scale.exp()
        # retrieve_logits = logit_scale * torch.matmul(sequence_output, visual_output.t())
        sim_matrix_global = logit_scale * torch.matmul(sequence_output, visual_output.t())
        return sim_matrix_global

    def _cross_similarity(self, sequence_output, visual_output, attention_mask, video_mask):
        sequence_output, visual_output = sequence_output.contiguous(), visual_output.contiguous()

        b_text, s_text, h_text = sequence_output.size()
        b_visual, s_visual, h_visual = visual_output.size()

        retrieve_logits_list = []

        step_size = b_text      # set smaller to reduce memory cost
        split_size = [step_size] * (b_text // step_size)
        release_size = b_text - sum(split_size)
        if release_size > 0:
            split_size += [release_size]

        # due to clip text branch retrun the last hidden
        attention_mask = torch.ones(sequence_output.size(0), 1)\
            .to(device=attention_mask.device, dtype=attention_mask.dtype)

        sequence_output_splits = torch.split(sequence_output, split_size, dim=0)
        attention_mask_splits = torch.split(attention_mask, split_size, dim=0)
        for i in range(len(split_size)):
            sequence_output_row = sequence_output_splits[i]
            attention_mask_row = attention_mask_splits[i]
            sequence_output_l = sequence_output_row.unsqueeze(1).repeat(1, b_visual, 1, 1)
            sequence_output_l = sequence_output_l.view(-1, s_text, h_text)
            attention_mask_l = attention_mask_row.unsqueeze(1).repeat(1, b_visual, 1)
            attention_mask_l = attention_mask_l.view(-1, s_text)

            step_truth = sequence_output_row.size(0)
            visual_output_r = visual_output.unsqueeze(0).repeat(step_truth, 1, 1, 1)
            visual_output_r = visual_output_r.view(-1, s_visual, h_visual)
            video_mask_r = video_mask.unsqueeze(0).repeat(step_truth, 1, 1)
            video_mask_r = video_mask_r.view(-1, s_visual)

            cross_output, pooled_output, concat_mask = \
                self._get_cross_output(sequence_output_l, visual_output_r, attention_mask_l, video_mask_r)
            retrieve_logits_row = self.similarity_dense(pooled_output).squeeze(-1).view(step_truth, b_visual)

            retrieve_logits_list.append(retrieve_logits_row)

        retrieve_logits = torch.cat(retrieve_logits_list, dim=0)
        return retrieve_logits

    def get_similarity_logits(self, sequence_hidden,sequence_hidden_coarse, visual_hidden,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse, attention_mask, video_mask, shaped=False, loose_type=False):
        if shaped is False:
            attention_mask = attention_mask.view(-1, attention_mask.shape[-1])
            video_mask = video_mask.view(-1, video_mask.shape[-1])

        # contrastive_direction = ()
        if loose_type:
            assert self.sim_header in ["meanP", "seqLSTM", "seqTransf"]
            sim_matrix_global, sim_matrix_semantic = self._loose_similarity(sequence_hidden,sequence_hidden_coarse, visual_hidden,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse, attention_mask, video_mask, sim_header=self.sim_header)
        else:
            assert self.sim_header in ["tightTransf"]
            retrieve_logits = self._cross_similarity(sequence_hidden,sequence_hidden_coarse, visual_hidden,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse, attention_mask, video_mask, )

        return sim_matrix_semantic, sim_matrix_global #, sim_matrix_semantic #, contrastive_direction
    
    def get_final_similarity(self,sequence_hidden,sequence_hidden_coarse, visual_hidden,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse, attention_mask, video_mask, shaped=False, loose_type=False):
        sim_matrix_semantic, sim_matrix_global = self.get_similarity_logits(sequence_hidden,sequence_hidden_coarse, visual_hidden,visual_hidden_Coarse,visual_multi_hidden,visual_multi_hidden_Coarse, attention_mask, video_mask, shaped=shaped, loose_type=loose_type)
        return self.frame_match_weight * sim_matrix_semantic + (1-self.frame_match_weight) * sim_matrix_global
    
    def get_frame_similarity(self, sequence_output, visual_output, attention_mask, video_mask):
        # sequence_output shape is (bs, 1, hid_dim), visual_output shape here is (bs, max_frames, hid_dim)
        # using ffn network change sequence_output shape from (bs, 1, hid_dim) to (bs, max_frames, hid_dim)

        sequence_output = sequence_output / sequence_output.norm(dim=-1, keepdim=True)
        visual_output = visual_output / visual_output.norm(dim=-1, keepdim=True)

        video_mask_un = video_mask.to(dtype=torch.bool).unsqueeze(-1)
        ##################################################################
        ## should change to visual matmul sequence due to match in test ##
        ##################################################################
        similarity_matrix = torch.einsum('mjk,nlk->mjn', visual_output, sequence_output)  # shape here should be(bs_v, max_frames, bs_s)

        similarity_matrix_weight = similarity_matrix * video_mask_un  # video_mask shape here is (bs_v, max_frames, 1)
        ##########  softmax nomalize  ####################
        similarity_matrix_weight = similarity_matrix_weight / similarity_matrix_weight.norm(dim=1, keepdim=True)
        similarity_matrix_weight = similarity_matrix_weight.masked_fill_(~video_mask_un, -1e18)
        similarity_matrix_weight = torch.softmax(4*similarity_matrix_weight, dim=1) # normalization between frames for each frame
        similarity_matrix = similarity_matrix_weight * similarity_matrix
        similarity_matrix = torch.sum(similarity_matrix, dim=1)
        ##########  softmax nomalize  ####################

        similarity_matrix = similarity_matrix.T # transpose here due to before transpose
        logit_scale = self.clip.logit_scale.exp()
        similarity_matrix = logit_scale * similarity_matrix
        # print("similarity maxtrix.shape: ", similarity_matrix.shape)

        return similarity_matrix

    def get_frame_selectedcls_similarity(self, sequence_output, visual_output, attention_mask, video_mask):
        sequence_output = sequence_output / sequence_output.norm(dim=-1, keepdim=True) # shape here is (bs, 1, hid_dim)
        visual_output = visual_output / visual_output.norm(dim=-1, keepdim=True) # shape here is (bs, max_frames, hid_dim)

        video_mask_un = video_mask.to(dtype=torch.bool).unsqueeze(-1)
        ##################################################################
        ## should change to visual matmul sequence due to match in test ##
        ##################################################################
        similarity_matrix = torch.einsum('mjk,nlk->mjln', visual_output, sequence_output)  # shape here should be(bs_v, max_frames, 2, bs_s)


        ########## softmax nomalize per frame ############
        similarity_matrix_weight = F.normalize(similarity_matrix, dim=2)
        similarity_matrix_weight = torch.softmax(4*similarity_matrix_weight, dim=2)
        similarity_matrix = similarity_matrix_weight * similarity_matrix
        similarity_matrix = torch.sum(similarity_matrix, dim=2)
        ########## softmax nomalize per frame ############

        ##########  softmax nomalize  ####################
        similarity_matrix_weight = similarity_matrix * video_mask_un  # video_mask shape here is (bs_v, max_frames, 1)
        similarity_matrix_weight = similarity_matrix_weight / similarity_matrix_weight.norm(dim=1, keepdim=True)
        similarity_matrix_weight = similarity_matrix_weight.masked_fill_(~video_mask_un, -1e18)
        similarity_matrix_weight = torch.softmax(4*similarity_matrix_weight, dim=1) # normalization between frames for each frame
        similarity_matrix = similarity_matrix_weight * similarity_matrix
        similarity_matrix = torch.sum(similarity_matrix, dim=1)
        ##########  softmax nomalize  ####################

        similarity_matrix = similarity_matrix.T # transpose here due to before transpose
        logit_scale = self.clip.logit_scale.exp()
        similarity_matrix = logit_scale * similarity_matrix
        # print("similarity maxtrix.shape: ", similarity_matrix.shape)

        return similarity_matrix

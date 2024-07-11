# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import asyncio
import os
import threading
from functools import partial

import torch
import torch.multiprocessing as mp
from omegaconf.omegaconf import OmegaConf, open_dict

from nemo.collections.nlp.models.information_retrieval.megatron_mamba_embedding_model import (
    MegatronMambaEmbeddingModel,
)
from nemo.collections.nlp.modules.common.text_generation_server import MegatronServer
from nemo.collections.nlp.modules.common.text_generation_utils import generate
from nemo.collections.nlp.parts.megatron_trainer_builder import MegatronLMPPTrainerBuilder
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.model_utils import inject_model_parallel_rank

try:
    from megatron.core import parallel_state

    HAVE_MEGATRON_CORE = True
except (ImportError, ModuleNotFoundError):

    HAVE_MEGATRON_CORE = False

mp.set_start_method("spawn", force=True)


def use_inference_server(cfg, model, trainer):
    if not HAVE_MEGATRON_CORE:
        raise ValueError('Megatron-core needs to be installed to use this feature!')

    from nemo.collections.nlp.modules.common.megatron_web_server import get_chatbot_demo, get_demo

    trainer.test(model, dataloaders=None)

    if parallel_state.is_pipeline_first_stage() and parallel_state.get_tensor_model_parallel_rank() == 0:
        if cfg.web_server:
            if cfg.chat:
                defaults = {
                    'user': cfg.chatbot_config.user,
                    'assistant': cfg.chatbot_config.assistant,
                    'system': cfg.chatbot_config.system,
                }
                web_ui = partial(
                    get_chatbot_demo,
                    defaults=defaults,
                    value=cfg.chatbot_config.value,
                    attributes=cfg.chatbot_config.attributes,
                )
            else:
                web_ui = get_demo
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=web_ui,
                daemon=True,
                args=(cfg.share, cfg.username, cfg.password, cfg.port, cfg.web_port, loop),
            )
            thread.start()
        server = MegatronServer(model.cuda())
        server.run("0.0.0.0", port=cfg.port)

    while True:
        choice = torch.cuda.LongTensor(1)
        torch.distributed.broadcast(choice, 0)
        if choice[0].item() == 0:
            generate(model.cuda())


@hydra_runner(config_path="conf", config_name="megatron_mamba_embedder_generate_config")
def main(cfg) -> None:
    logging.info("\n\n************** Experiment configuration ***********")
    logging.info(f"\n{OmegaConf.to_yaml(cfg)}")
    trainer = MegatronLMPPTrainerBuilder(cfg).create_trainer()

    model_cfg = MegatronMambaEmbeddingModel.merge_inference_cfg(cfg.model.restore_from_path, cfg)

    with open_dict(model_cfg):
        model_cfg.data.return_output_tensors = True
        model_cfg.post_process = False

    model = MegatronMambaEmbeddingModel.restore_from(cfg.model.restore_from_path, model_cfg, trainer=trainer)

    if not cfg.model.get('use_flash_attention', False):
        cfg.inference.compute_attention_mask = True
    config = OmegaConf.to_container(cfg.inference, resolve=True)
    model.set_inference_config(config)

    if not cfg.server:
        trainer.test(model)
    else:
        use_inference_server(cfg, model, trainer)


if __name__ == "__main__":
    main()

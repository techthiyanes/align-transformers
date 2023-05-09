#!/usr/bin/env python
# coding: utf-8
import os, random, argparse, sys, torch
from models.llama.modelings_alignable_llama import AlignableLlamaForCausalLM
from models.configuration_alignable_model import AlignableLlamaConfig
from trainer import AlpacaAligner, CACHE_DIR
from counterfacutal_datasets import prepare_dataloader
from transformers import (
    set_seed,
    AutoTokenizer,
    AutoConfig,
    get_linear_schedule_with_warmup
)
from torch.utils.data import DataLoader, SequentialSampler

from transformers.utils import logging
logging.set_verbosity_info()
logger = logging.get_logger("transformers")

if __name__ == '__main__':
    is_notebook = False
    try:
        cmd = argparse.ArgumentParser('The testing components of')
        cmd.add_argument('--train_batch_size', default=128, type=int, help='training batch size')
        cmd.add_argument('--eval_batch_size', default=128, type=int, help='training batch size')
        cmd.add_argument('--lr', default=0.01, type=float, help='learning rate')
        cmd.add_argument(
            '--encoder_config_path', 
            type=str, help='path to the encoder config'
        )
        cmd.add_argument(
            '--decoder_config_path', 
            type=str, help='path to the decoder config'
        )
        cmd.add_argument('--max_seq_len', default=512, type=int)
        cmd.add_argument('--seed', default=42, type=int)
        cmd.add_argument('--gradient_accumulation_steps', default=1, type=int)
        cmd.add_argument('--output_dir', required=True, type=str, help='save dir')
        cmd.add_argument('--local_rank', default=-1, type=int, help='multi gpu training')
        cmd.add_argument('--epochs', default=10, type=int, help='training epochs')
        cmd.add_argument('--model_path', type=str, required=False, default="../alpaca_7b/")
        cmd.add_argument('--warm_up', type=float, default=0.1)
        cmd.add_argument('--is_wandb', default=False, action='store_true')
        cmd.add_argument('--wandb_username', type=str, default="")
        cmd.add_argument('--bf16', default=False, action='store_true')
        cmd.add_argument('--log_step', default=10, type=int)
        cmd.add_argument('--valid_steps', default=500, type=int)
        cmd.add_argument('--early_stopping', default=5, type=int)
        cmd.add_argument('--device', default="cuda", type=str, help='')
        cmd.add_argument('--do_align', default=False, action='store_true')
        cmd.add_argument('--do_eval', default=False, action='store_true')
        cmd.add_argument('--do_test', default=False, action='store_true')
        cmd.add_argument('--n_training_examples', default=10000, type=int)
        cmd.add_argument('--n_eval_examples', default=1000, type=int)
        cmd.add_argument('--task_name', default="pricing_tag_lb", type=str, help='')
        
        args = cmd.parse_args(sys.argv[1:])
    except:
        assert False

set_seed(args.seed)

###################
# data loaders
###################
tokenizer = AutoTokenizer.from_pretrained(
    pretrained_model_name_or_path=args.model_path,
    cache_dir=CACHE_DIR
)
prealign_dataloader, train_dataloader, eval_dataloader, test_dataloader = prepare_dataloader(args, tokenizer)

###################
# model object loading
###################
das_config = AlignableLlamaConfig.from_pretrained(
    os.path.join(args.model_path, "das_config")
)
alignment_config = {
    'layer': das_config.das_layer,
    "token_range" : [
        das_config.das_token_range[0], 
        das_config.das_token_range[1], 
    ]
}
logger.info(f"alignment_config = {alignment_config}")

run_name = f"alpaca-7B.task.{args.task_name}."\
           f"seed.{args.seed}.intl.{alignment_config['layer']}.intr.{alignment_config['token_range'][0]}."\
           f"{alignment_config['token_range'][1]}"

is_master = True
if not os.path.exists(args.output_dir) and is_master:
    os.mkdir(args.output_dir)
os.environ["WANDB_PROJECT"] = f"Boundless-DAS"
output_dir = os.path.join(args.output_dir, run_name)
if not os.path.exists(output_dir) and is_master:
    os.mkdir(output_dir)
    
# now we check whether we can skip ...
# if there is last, we need to skip!
file_path = os.path.join(args.output_dir, run_name, "pytorch-rotate-last.bin")
das_config.save_pretrained(os.path.join(args.output_dir, run_name, "das_config"))
if not os.path.isfile(file_path):
    logger.info(f"Loading Pretrained LLM with bf16 = {args.bf16}...")
    model = AlignableLlamaForCausalLM.from_pretrained(
        args.model_path,
        alignment_config=alignment_config,
        torch_dtype=torch.bfloat16 if args.bf16 else None
    )

    # set off the gradients among all other layers.
    for name, param in model.named_parameters():
        if "rotate_layer" not in name and "intervention_boundaries" not in name:
            param.requires_grad = False
        else:
            logger.info(f"Requiring gradients on layer: {name}")

    t_total = int(len(train_dataloader) * args.epochs)
    warm_up_steps = args.warm_up * t_total
    optimizer = torch.optim.Adam(
        [{'params': model.model.rotate_layer.parameters()},
        {'params': model.model.intervention_boundaries, 'lr': 1e-2}],
        lr=args.lr
    )
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=warm_up_steps,
        num_training_steps=t_total
    )

    device = "cuda"
    model.to(device)

    ###################
    # trainer loading
    ###################
    aligner = AlpacaAligner(
        model,
        logger=logger,
        args=args,
        is_master=is_master,
        n_gpu=torch.cuda.device_count(),
        model_name=run_name,
        device=device
    )

    # Prealign Eval is a must
    aligner.prealign_eval(prealign_dataloader, output_dir)

    # Train
    if args.do_align:
        aligner.train(
            train_dataloader, eval_dataloader, test_dataloader,
            optimizer, scheduler, 
            log_step=args.log_step, valid_steps=args.valid_steps,
            output_dir=output_dir, epochs=args.epochs, 
            gradient_accumulation_steps=args.gradient_accumulation_steps,
        )
else:
    logger.info("Skipping! Found previously finished training run for this experiment.")
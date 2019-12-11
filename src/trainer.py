#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Nov 29 09:53:55 2019

@author: weetee
"""
import os
import torch
import torch.nn as nn
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_
from .model.modeling_bert import BertModel
from .preprocessing_funcs import load_dataloaders
from .train_funcs import Two_Headed_Loss, load_state, load_results, evaluate_
from .misc import save_as_pickle, load_pickle
import matplotlib.pyplot as plt
import time
import logging

logging.basicConfig(format='%(asctime)s [%(levelname)s]: %(message)s', \
                    datefmt='%m/%d/%Y %I:%M:%S %p', level=logging.INFO)
logger = logging.getLogger(__file__)

def train_and_fit(args):
    
    if args.fp16:    
        from apex import amp
    else:
        amp = None
    
    cuda = torch.cuda.is_available()
    
    train_loader = load_dataloaders(args)
    train_len = len(train_loader)
    logger.info("Loaded %d pre-training samples." % train_len)
    
    net = BertModel.from_pretrained('bert-base-uncased', force_download=False)
    tokenizer = load_pickle("BERT_tokenizer.pkl")
    net.resize_token_embeddings(len(tokenizer)) 
    if cuda:
        net.cuda()
        
    logger.info("FREEZING MOST HIDDEN LAYERS...")
    unfrozen_layers = ["classifier", "pooler", "encoder.layer.11", "blanks_linear", "lm_linear", "cls"]
    for name, param in net.named_parameters():
        if not any([layer in name for layer in unfrozen_layers]):
            print("[FROZE]: %s" % name)
            param.requires_grad = False
        else:
            print("[FREE]: %s" % name)
            param.requires_grad = True
       
    criterion = Two_Headed_Loss(lm_ignore_idx=tokenizer.pad_token_id)
    optimizer = optim.Adam([{"params":net.parameters(), "lr": args.lr}])
    
    scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=[2,4,6,8,12,15,18,20,22,\
                                                                      24,26,30], gamma=0.8)
    
    start_epoch, best_pred, amp_checkpoint = load_state(net, optimizer, scheduler, args, load_best=False)
    
    if (args.fp16) and (amp is not None):
        logger.info("Using fp16...")
        net, optimizer = amp.initialize(net, optimizer, opt_level='O2')
        if amp_checkpoint is not None:
            amp.load_state_dict(amp_checkpoint)
        scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=[2,4,6,8,12,15,18,20,22,\
                                                                          24,26,30], gamma=0.8)
    
    losses_per_epoch, accuracy_per_epoch = load_results(args.model_no)
    
    logger.info("Starting training process...")
    pad_id = tokenizer.pad_token_id
    mask_id = tokenizer.mask_token_id
    update_size = len(train_loader)//10
    for epoch in range(start_epoch, args.num_epochs):
        start_time = time.time()
        net.train(); total_loss = 0.0; losses_per_batch = []; total_acc = 0.0; lm_accuracy_per_batch = []
        for i, data in enumerate(train_loader, 0):
            x, masked_for_pred, e1_e2_start, Q, blank_labels, _,_,_,_,_ = data
            masked_for_pred = masked_for_pred[(masked_for_pred != pad_id)]
            attention_mask = (x != pad_id).float()
            token_type_ids = torch.zeros((x.shape[0], x.shape[1])).long()

            if cuda:
                x = x.cuda(); masked_for_pred = masked_for_pred.cuda(); Q = Q.cuda().float()
                blank_labels = blank_labels.cuda().float()
                attention_mask = attention_mask.cuda()
                token_type_ids = token_type_ids.cuda()
                
            x = x.long()
            blanks_logits, lm_logits = net(x, token_type_ids=token_type_ids, attention_mask=attention_mask, Q=Q,\
                          e1_e2_start=e1_e2_start)
            lm_logits = lm_logits[(x == mask_id)]
            
            #return lm_logits, blanks_logits, masked_for_pred, blank_labels, tokenizer # for debugging now
            
            loss = criterion(lm_logits, blanks_logits, masked_for_pred, blank_labels)
            loss = loss/args.gradient_acc_steps
            
            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()
            
            if args.fp16:
                grad_norm = torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_norm)
            else:
                grad_norm = clip_grad_norm_(net.parameters(), args.max_norm)
            
            if (i % args.gradient_acc_steps) == 0:
                optimizer.step()
                optimizer.zero_grad()
            
            total_loss += loss.item()
            total_acc += evaluate_(lm_logits, blanks_logits, masked_for_pred, blank_labels, \
                                   tokenizer, print_=False)[0]
            
            if (i % update_size) == (update_size - 1):
                losses_per_batch.append(args.gradient_acc_steps*total_loss/update_size)
                lm_accuracy_per_batch.append(total_acc/update_size)
                print('[Epoch: %d, %5d/ %d points] total loss, lm accuracy per batch: %.3f, %.3f' %
                      (epoch + 1, (i + 1), train_len, losses_per_batch[-1], lm_accuracy_per_batch[-1]))
                total_loss = 0.0; total_acc = 0.0
        
        scheduler.step()
        losses_per_epoch.append(sum(losses_per_batch)/len(losses_per_batch))
        accuracy_per_epoch.append(sum(lm_accuracy_per_batch)/len(lm_accuracy_per_batch))
        print("Epoch finished, took %.2f seconds." % (time.time() - start_time))
        print("Losses at Epoch %d: %.7f" % (epoch + 1, losses_per_epoch[-1]))
        print("Accuracy at Epoch %d: %.7f" % (epoch + 1, accuracy_per_epoch[-1]))
        
        if accuracy_per_epoch[-1] > best_pred:
            best_pred = accuracy_per_epoch[-1]
            torch.save({
                    'epoch': epoch + 1,\
                    'state_dict': net.state_dict(),\
                    'best_acc': accuracy_per_epoch[-1],\
                    'optimizer' : optimizer.state_dict(),\
                    'scheduler' : scheduler.state_dict(),\
                    'amp': amp.state_dict() if amp is not None else amp
                }, os.path.join("./data/" , "test_model_best_%d.pth.tar" % args.model_no))
        
        if (epoch % 1) == 0:
            save_as_pickle("test_losses_per_epoch_%d.pkl" % args.model_no, losses_per_epoch)
            save_as_pickle("test_accuracy_per_epoch_%d.pkl" % args.model_no, accuracy_per_epoch)
            torch.save({
                    'epoch': epoch + 1,\
                    'state_dict': net.state_dict(),\
                    'best_acc': accuracy_per_epoch[-1],\
                    'optimizer' : optimizer.state_dict(),\
                    'scheduler' : scheduler.state_dict(),\
                    'amp': amp.state_dict() if amp is not None else amp
                }, os.path.join("./data/" , "test_checkpoint_%d.pth.tar" % args.model_no))
    
    logger.info("Finished Training!")
    fig = plt.figure(figsize=(20,20))
    ax = fig.add_subplot(111)
    ax.scatter([e for e in range(len(losses_per_epoch))], losses_per_epoch)
    ax.tick_params(axis="both", length=2, width=1, labelsize=14)
    ax.set_xlabel("Epoch", fontsize=22)
    ax.set_ylabel("Training Loss per batch", fontsize=22)
    ax.set_title("Training Loss vs Epoch", fontsize=32)
    plt.savefig(os.path.join("./data/" ,"loss_vs_epoch_%d.png" % args.model_no))
    
    fig2 = plt.figure(figsize=(20,20))
    ax2 = fig2.add_subplot(111)
    ax2.scatter([e for e in range(len(accuracy_per_epoch))], accuracy_per_epoch)
    ax2.tick_params(axis="both", length=2, width=1, labelsize=14)
    ax2.set_xlabel("Epoch", fontsize=22)
    ax2.set_ylabel("Test Masked LM Accuracy", fontsize=22)
    ax2.set_title("Test Masked LM Accuracy vs Epoch", fontsize=32)
    plt.savefig(os.path.join("./data/" ,"accuracy_vs_epoch_%d.png" % args.model_no))
    
    return net
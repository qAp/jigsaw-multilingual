"""
Classify based on original language comments w/ k-folds validation
"""
import time
import pandas as pd
from itertools import starmap
from random import shuffle
from functools import partial
import multiprocessing as mp
import numpy as np
import torch
from transformers import AutoTokenizer, AutoModel, AutoConfig
from sklearn.metrics import roc_auc_score
from apex import amp
from tqdm import trange
from preprocessor import (generate_train_kfolds_indices,
                          get_id_text_distill_label_from_csv,
                          get_id_text_label_from_csv)
from torch_helpers import EMA, save_model

USE_AMP = True
USE_MULTI_GPU = False
USE_PSEUDO_LABELS = True
PSEUDO_TEXT_PATH = 'data/test.csv'
PSEUDO_LABEL_PATH = 'data/test/bert-base-pl1.csv'
SAVE_MODEL = True
OUTPUT_DIR = 'models/multilingual'
TRAIN_CSV_PATH = 'data/validation.csv'
PRETRAINED_MODEL = 'distilbert-base-multilingual-cased'
NUM_GPUS = 2  # Set to 1 if using AMP (doesn't seem to play nice with 1080 Ti)
MAX_CORES = 8  # limit MP calls to use this # cores at most
BASE_MODEL_OUTPUT_DIM = 768  # hidden layer dimensions
INTERMEDIATE_HIDDEN_UNITS = 1
MAX_SEQ_LEN = 200  # max sequence length for input strings: gets padded/truncated
NUM_EPOCHS = 6
BATCH_SIZE = 64
USE_EMA = False
EMA_DECAY = 0.999


class ClassifierHead(torch.nn.Module):
    """
    Bert base with a Linear layer plopped on top of it
    - connects the max pool of the last hidden layer with the FC
    """

    def __init__(self, base_model):
        super(ClassifierHead, self).__init__()
        self.base_model = base_model
        self.cnn = torch.nn.Conv1d(BASE_MODEL_OUTPUT_DIM, INTERMEDIATE_HIDDEN_UNITS, kernel_size=1)
        self.fc = torch.nn.Linear(BASE_MODEL_OUTPUT_DIM, INTERMEDIATE_HIDDEN_UNITS)

    def forward(self, x, freeze=True):
        if freeze:
            with torch.no_grad():
                hidden_states = self.base_model(x)[0]
        else:
            hidden_states = self.base_model(x)[0]

        hidden_states = hidden_states.permute(0, 2, 1)
        cnn_states = self.cnn(hidden_states)
        cnn_states = cnn_states.permute(0, 2, 1)
        logits, _ = torch.max(cnn_states, 1)

        # logits = self.fc(hidden_states[:, -1, :])
        prob = torch.nn.Sigmoid()(logits)
        return prob


def train(model, train_tuple, loss_fn, opt, curr_epoch, ema, use_gpu_id, fold_id):
    """ Train """
    # Shuffle train indices for current epoch, batching
    all_features, all_labels, _ = train_tuple
    train_indices = list(range(len(all_labels)))

    shuffle(train_indices)
    train_features = all_features[train_indices]
    train_labels = all_labels[train_indices]

    # switch to finetune
    if curr_epoch == 1:
        for g in opt.param_groups:
            g['lr'] = 1e-5

    model.train()
    with trange(0, len(train_indices), BATCH_SIZE,
                desc='{} - {}'.format(fold_id, curr_epoch),
                position=use_gpu_id) as t:
        for batch_idx_start in t:
            opt.zero_grad()
            batch_idx_end = min(batch_idx_start + BATCH_SIZE, len(train_indices))

            batch_features = torch.tensor(train_features[batch_idx_start:batch_idx_end]).cuda()
            batch_labels = torch.tensor(train_labels[batch_idx_start:batch_idx_end]).float().cuda().unsqueeze(-1)

            if curr_epoch < 1:
                preds = model(batch_features, freeze=True)
            else:
                preds = model(batch_features, freeze=False)
            loss = loss_fn(preds, batch_labels)

            if USE_AMP:
                with amp.scale_loss(loss, opt) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            opt.step()

            if USE_EMA:
                # Update EMA shadow parameters on every back pass
                for name, param in model.named_parameters():
                    if param.requires_grad:
                        ema.update(name, param.data)


def evaluate(model, val_tuple):
    # Evaluate validation AUC
    val_features, val_labels, val_ids = val_tuple

    model.eval()
    val_preds = []
    with torch.no_grad():
        for batch_idx_start in range(0, len(val_ids), BATCH_SIZE):
            batch_idx_end = min(batch_idx_start + BATCH_SIZE, len(val_ids))
            batch_features = torch.tensor(val_features[batch_idx_start:batch_idx_end]).cuda()
            batch_preds = model(batch_features)
            val_preds.append(batch_preds.cpu())

        val_preds = np.concatenate(val_preds)
        val_roc_auc_score = roc_auc_score(val_labels, val_preds)
    return val_roc_auc_score, val_preds


def main_driver(fold_id, fold_indices,
                all_tuple,
                pseudo_tuple,
                gpu_id_queue):
    use_gpu_id = gpu_id_queue.get()
    fold_start_time = time.time()
    import os
    os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
    os.environ['CUDA_VISIBLE_DEVICES'] = str(use_gpu_id)
    print('Fold {} training: GPU_ID{}'.format(fold_id, use_gpu_id))
    pretrained_config = AutoConfig.from_pretrained(PRETRAINED_MODEL,
                                                   output_hidden_states=True)
    pretrained_base = AutoModel.from_pretrained(PRETRAINED_MODEL, config=pretrained_config).cuda()
    classifier = ClassifierHead(pretrained_base).cuda()

    if USE_EMA:
        ema = EMA(EMA_DECAY)
        for name, param in classifier.named_parameters():
            if param.requires_grad:
                ema.register(name, param.data)
    else:
        ema = None

    loss_fn = torch.nn.BCELoss()
    opt = torch.optim.Adam(classifier.parameters(), lr=1e-3)

    if USE_AMP:
        amp.register_float_function(torch, 'sigmoid')
        classifier, opt = amp.initialize(classifier, opt, opt_level='O1', verbosity=0)

    if USE_MULTI_GPU:
        classifier = torch.nn.DataParallel(classifier)

    all_features, all_labels, all_ids = all_tuple
    pseudo_features, pseudo_labels, pseudo_ids = pseudo_tuple

    train_indices, val_indices = fold_indices
    train_features, train_labels = all_features[train_indices], all_labels[train_indices]
    val_features, val_labels, val_ids = all_features[val_indices], all_labels[val_indices], all_ids[val_indices]

    if USE_PSEUDO_LABELS:
        train_features = np.concatenate([pseudo_features, train_features])
        train_labels = np.concatenate([pseudo_labels, train_labels])
        train_indices = list(range(len(train_labels)))

    if fold_id == 0:
        print('train size: {}, val size: {}'.format(len(train_indices), len(val_indices)))

    epoch_eval_score = []
    epoch_val_id_to_pred = []
    best_auc = -1
    for curr_epoch in range(NUM_EPOCHS):
        # Shuffle train indices for current epoch, batching
        shuffle(train_indices)

        # switch to finetune
        if curr_epoch == 1:
            for g in opt.param_groups:
                g['lr'] = 1e-5

        train(classifier,
              [train_features, train_labels, None],
              loss_fn,
              opt,
              curr_epoch,
              ema,
              use_gpu_id,
              fold_id)

        # Evaluate validation fold
        epoch_auc, val_preds = evaluate(classifier, [val_features, val_labels, val_ids])
        print('Fold {}, Epoch {} - AUC: {:.4f}'.format(fold_id, curr_epoch, epoch_auc))
        epoch_eval_score.append(epoch_auc)
        epoch_val_id_to_pred.append({val_id: val_pred for val_id, val_pred in zip(val_ids, val_preds)})

        if epoch_auc > best_auc and SAVE_MODEL and fold_id == 0:
            print('Translated AUC increased; saving model')
            best_auc = epoch_auc
            save_model(os.path.join(OUTPUT_DIR, PRETRAINED_MODEL), classifier, pretrained_config, tokenizer)

    if USE_EMA and SAVE_MODEL and fold_id == 0:
        # Load EMA parameters and evaluate once again
        for name, param in classifier.named_parameters():
            if param.requires_grad:
                param.data = ema.get(name)
        epoch_auc = evaluate(classifier, [val_features, val_labels, val_ids])
        print('EMA ->Fold {}, Epoch {} - AUC: {:.4f}'.format(fold_id, curr_epoch, epoch_auc))
        save_model(os.path.join(OUTPUT_DIR, '{}_ema'.format(PRETRAINED_MODEL)), classifier, pretrained_config,
                   tokenizer)

    gpu_id_queue.put(use_gpu_id)
    print('Fold {} run-time: {:.4f}'.format(fold_id, time.time() - fold_start_time))
    return epoch_eval_score, epoch_val_id_to_pred


if __name__ == '__main__':
    start_time = time.time()
    print('Using model: {}'.format(PRETRAINED_MODEL))
    all_ids, all_strings, all_labels = get_id_text_label_from_csv(TRAIN_CSV_PATH)
    pseudo_ids, pseudo_strings, pseudo_labels = get_id_text_distill_label_from_csv(PSEUDO_TEXT_PATH,
                                                                                   PSEUDO_LABEL_PATH,
                                                                                   'content')
    pseudo_features = None

    fold_indices = generate_train_kfolds_indices(all_strings)

    # use MP to batch encode the raw feature strings into Bert token IDs
    tokenizer = AutoTokenizer.from_pretrained(PRETRAINED_MODEL)
    if 'gpt' in PRETRAINED_MODEL:  # GPT2 pre-trained tokenizer doesn't set a padding token
        tokenizer.add_special_tokens({'pad_token': '<|endoftext|>'})

    encode_partial = partial(tokenizer.encode,
                             max_length=MAX_SEQ_LEN,
                             pad_to_max_length=True,
                             add_special_tokens=True)
    print('Encoding raw strings into model-specific tokens')
    with mp.Pool(MAX_CORES) as p:
        all_features = np.array(p.map(encode_partial, all_strings))
        if USE_PSEUDO_LABELS:
            pseudo_features = np.array(p.map(encode_partial, pseudo_strings))

    print('Starting kfold training')
    with mp.Pool(NUM_GPUS, maxtasksperchild=1) as p:
        # prime GPU ID queue with IDs
        gpu_id_queue = mp.Manager().Queue()
        [gpu_id_queue.put(i) for i in range(NUM_GPUS)]

        results = p.starmap(main_driver,
                            ((fold_id,
                              curr_fold_indices,
                              [all_features, all_labels, all_ids],
                              [pseudo_features, pseudo_labels, pseudo_ids],
                              gpu_id_queue) for (fold_id, curr_fold_indices) in enumerate(fold_indices)))

    mean_score = np.mean(np.stack([x[0] for x in results]), axis=0)
    with np.printoptions(precision=4, suppress=True):
        print('Mean fold ROC_AUC_SCORE: {}'.format(mean_score))

    for curr_epoch in range(NUM_EPOCHS):
        oof_preds = {}
        [oof_preds.update(x[1][curr_epoch]) for x in results]
        oof_preds = pd.DataFrame.from_dict(oof_preds, orient='index').reset_index()
        oof_preds.columns = ['id', 'toxic']
        oof_preds.sort_values(by='id') \
            .to_csv('data/oof/multilingual_{}_{}_{}.csv'.format(PRETRAINED_MODEL,
                                                                curr_epoch + 1,
                                                                MAX_SEQ_LEN),
                    index=False)

    print('Elapsed time: {}'.format(time.time() - start_time))
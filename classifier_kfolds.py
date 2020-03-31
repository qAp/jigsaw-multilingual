"""
Classify w/ k-folds validation
- for validation, we flag only samples we're interested in at TRAIN_CSV_PATHS
  since we do not want to factor english language samples
- Note: we're also k-folding on the base english dataset (better to use all and k-fold only on non-english?)
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
from torch_helpers import EMA, save_model, layerwise_lr_decay

USE_AMP = True
USE_MULTI_GPU = False
SAVE_MODEL = True
USE_EMA = False
USE_LR_DECAY = False
# PATH TUPLE - (PATH, SAMPLE_FRAC, WHETHER_TO_USE_IN_VALIDATION)
TRAIN_CSV_PATHS = [['data/toxic_2018/combined.csv', 0.4, False],
                   ['data/validation.csv', 1., True]]
OUTPUT_DIR = 'models/kfolds/'
PRETRAINED_MODEL = 'xlm-roberta-large'
NUM_GPUS = 2  # Set to 1 if using AMP (doesn't seem to play nice with 1080 Ti)
MAX_CORES = 8  # limit MP calls to use this # cores at most
BASE_MODEL_OUTPUT_DIM = 1024  # hidden layer dimensions
INTERMEDIATE_HIDDEN_UNITS = 1
MAX_SEQ_LEN = 200  # max sequence length for input strings: gets padded/truncated
NUM_EPOCHS = 4
BATCH_SIZE = 24
ACCUM_FOR = 2
EMA_DECAY = 0.999
LR_DECAY_FACTOR = 0.75
LR_DECAY_START = 1e-3
LR_FINETUNE = 1e-5


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

    # switch to finetune - only lower for those > finetune LR
    # i.e., lower layers might have even smaller LR
    if curr_epoch == 1:
        for g in opt.param_groups:
            g['lr'] = LR_FINETUNE

    model.train()
    iter = 0
    with trange(0, len(train_indices), BATCH_SIZE,
                desc='{} - {}'.format(fold_id, curr_epoch),
                position=use_gpu_id) as t:
        for batch_idx_start in t:
            iter += 1
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

            if iter % ACCUM_FOR == 0:
                opt.step()
                opt.zero_grad()

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
    if USE_LR_DECAY:
        parameters_update = layerwise_lr_decay(classifier, LR_DECAY_START, LR_DECAY_FACTOR)
        opt = torch.optim.Adam(parameters_update)
    else:
        opt = torch.optim.Adam(classifier.parameters(), lr=LR_DECAY_START)

    if USE_AMP:
        amp.register_float_function(torch, 'sigmoid')
        classifier, opt = amp.initialize(classifier, opt, opt_level='O1', verbosity=0)

    if USE_MULTI_GPU:
        classifier = torch.nn.DataParallel(classifier)

    all_features, all_labels, all_ids, all_val_flag = all_tuple

    train_indices, val_indices = fold_indices
    train_features, train_labels = all_features[train_indices], all_labels[train_indices]
    val_features, val_labels, val_ids, val_flag = \
        (all_features[val_indices], all_labels[val_indices], all_ids[val_indices], all_val_flag[val_indices])

    # trim down to those flagged for validation
    val_features, val_labels, val_ids = (val_features[val_flag], val_labels[val_flag], val_ids[val_flag])

    if fold_id == 0:
        print('train size: {}, val size: {}'.format(len(train_indices), len(val_features)))

    epoch_eval_score = []
    epoch_val_id_to_pred = []
    for curr_epoch in range(NUM_EPOCHS):
        # Shuffle train indices for current epoch, batching
        shuffle(train_indices)

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

        if fold_id == 0:
            print('Saving model')
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
    train_tuple = [get_id_text_label_from_csv(curr_path, sample_frac=curr_frac, add_label=curr_to_validate)
                   for curr_path, curr_frac, curr_to_validate in TRAIN_CSV_PATHS]
    all_ids = np.concatenate([x[0] for x in train_tuple])
    all_strings = np.concatenate([x[1] for x in train_tuple])
    all_labels = np.concatenate([x[2] for x in train_tuple])
    all_val_flag = np.concatenate([x[3] for x in train_tuple])

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

    print(all_ids.shape, all_features.shape, all_labels.shape, all_val_flag.shape)

    print('Starting kfold training')
    with mp.Pool(NUM_GPUS, maxtasksperchild=1) as p:
        # prime GPU ID queue with IDs
        gpu_id_queue = mp.Manager().Queue()
        [gpu_id_queue.put(i) for i in range(NUM_GPUS)]

        results = p.starmap(main_driver,
                            ((fold_id,
                              curr_fold_indices,
                              [all_features, all_labels, all_ids, all_val_flag],
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
            .to_csv('data/oof/kfolds_{}_{}_{}.csv'.format(PRETRAINED_MODEL,
                                                          curr_epoch + 1,
                                                          MAX_SEQ_LEN),
                    index=False)

    print('Elapsed time: {}'.format(time.time() - start_time))

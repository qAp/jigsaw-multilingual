"""
Baseline PyTorch classifier for Jigsaw Multilingual
- Assumes two separate train and val sets (i.e., no need for k-folds)
- Splits epochs between training the train set and the val set (i.e., 0.5 NUM_EPOCHS each)
"""
import os
import time
from itertools import starmap
from random import shuffle
from functools import partial
import multiprocessing as mp
import pandas as pd
import numpy as np
import torch
from transformers import AutoTokenizer, AutoModel, AutoConfig
from sklearn.metrics import roc_auc_score
from apex import amp
from tqdm import trange
from preprocessor import get_id_text_label_from_csv, LANG_MAPPING
from torch_helpers import EMA, save_model, layerwise_lr_decay

SAVE_MODEL = True
USE_AMP = True
USE_EMA = False
USE_PSEUDO = True  # Add pseudo labels to training dataset
USE_MULTI_GPU = False
USE_LR_DECAY = False
PRETRAINED_MODEL = 'xlm-roberta-large'
TRAIN_SAMPLE_FRAC = .4  # what % of training data to use
TRAIN_CSV_PATH = 'data/toxic_2018/pl_en.csv'
VAL_CSV_PATH = 'data/validation_en.csv'
PSEUDO_CSV_PATH = 'data/test9438.csv'
OUTPUT_DIR = 'models/multi_target'
NUM_GPUS = 2  # Set to 1 if using AMP (doesn't seem to play nice with 1080 Ti)
MAX_CORES = 24  # limit MP calls to use this # cores at most
BASE_MODEL_OUTPUT_DIM = 1024  # hidden layer dimensions
INTERMEDIATE_HIDDEN_UNITS = 7
MAX_SEQ_LEN = 200  # max sequence length for input strings: gets padded/truncated
NUM_EPOCHS = 6  # Half trained using train, half on val (+ PL)
BATCH_SIZE = 24
ACCUM_FOR = 2
SAVE_ITER = 100  # save every X iterations
EMA_DECAY = 0.999
LR_DECAY_FACTOR = 0.75
LR_DECAY_START = 1e-3
LR_FINETUNE = 1e-5

if not USE_MULTI_GPU:
    os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
    os.environ['CUDA_VISIBLE_DEVICES'] = '1'


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

        # logits = self.fc(hidden_states[:, 0, :])
        prob = torch.nn.Sigmoid()(logits)
        return prob


def train(model, config, train_tuple, loss_fn, opt, curr_epoch, ema):
    """ Train """
    # Shuffle train indices for current epoch, batching
    all_features, all_labels, all_lang_labels, all_ids = train_tuple
    train_indices = list(range(len(all_labels)))

    shuffle(train_indices)
    train_features = all_features[train_indices]
    train_labels = all_labels[train_indices]

    model.train()
    iter = 0
    running_total_loss = 0
    with trange(0, len(train_indices), BATCH_SIZE,
                desc='Epoch {}'.format(curr_epoch)) as t:
        for batch_idx_start in t:
            iter += 1
            batch_idx_end = min(batch_idx_start + BATCH_SIZE, len(train_indices))

            batch_features = torch.tensor(train_features[batch_idx_start:batch_idx_end]).cuda()
            batch_labels = torch.tensor(train_labels[batch_idx_start:batch_idx_end]).float().cuda()

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

            running_total_loss += loss.detach().cpu().numpy()
            t.set_postfix(loss=running_total_loss / iter)

            if iter % ACCUM_FOR == 0:
                opt.step()
                opt.zero_grad()

            if USE_EMA:
                # Update EMA shadow parameters on every back pass
                for name, param in model.named_parameters():
                    if param.requires_grad:
                        ema.update(name, param.data)

    # Save every specified step on last epoch
    if SAVE_MODEL:
        print('Saving epoch {} model'.format(curr_epoch))
        save_model(os.path.join(OUTPUT_DIR, PRETRAINED_MODEL, str(curr_epoch)),
                   model,
                   config,
                   tokenizer)


def evaluate(model, val_tuple):
    # Evaluate validation AUC
    val_features, val_labels, val_lang_labels, val_ids = val_tuple

    model.eval()
    val_preds = []
    with torch.no_grad():
        for batch_idx_start in range(0, len(val_ids), BATCH_SIZE):
            batch_idx_end = min(batch_idx_start + BATCH_SIZE, len(val_ids))
            batch_features = torch.tensor(val_features[batch_idx_start:batch_idx_end]).cuda()
            batch_preds = model(batch_features)
            val_preds.append(batch_preds.cpu())

        val_preds = np.concatenate(val_preds)
        val_preds = (val_preds * val_lang_labels).sum(1)
        print('Val preds shape: {}'.format(val_preds.shape))
        print('Val labels shape: {}'.format(val_labels.sum(1).shape))

        print(val_preds[:5])
        print(val_labels.sum(1)[:5])

        val_roc_auc_score = roc_auc_score(val_labels.sum(1), val_preds)
    return val_roc_auc_score


def main_driver(train_tuple, val_raw_tuple, val_translated_tuple, pseudo_tuple, tokenizer):
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

    list_raw_auc, list_translated_auc = [], []

    current_tuple = train_tuple
    for curr_epoch in range(NUM_EPOCHS):
        # switch to finetune - only lower for those > finetune LR
        # i.e., lower layers might have even smaller LR
        if curr_epoch == 1:
            print('Switching to fine-tune LR')
            for g in opt.param_groups:
                g['lr'] = LR_FINETUNE

        # Switch from toxic-2018 to the current mixed language dataset, halfway thru
        # if curr_epoch == NUM_EPOCHS // 2:
        #     print('Switching to training on validation set')
        #     print('Saving base English trained model')
        #     save_model(os.path.join(OUTPUT_DIR, PRETRAINED_MODEL, 'base_en'),
        #                classifier, pretrained_config, tokenizer)
        #     if USE_PSEUDO:
        #         current_tuple = pseudo_tuple
        #     else:
        #         current_tuple = val_raw_tuple

        train(classifier, pretrained_config, current_tuple, loss_fn, opt, curr_epoch, ema)

        epoch_raw_auc = evaluate(classifier, val_raw_tuple)
        epoch_translated_auc = evaluate(classifier, val_translated_tuple)
        print('Epoch {} - Raw: {:.4f}, Translated: {:.4f}'.format(curr_epoch, epoch_raw_auc, epoch_translated_auc))
        list_raw_auc.append(epoch_raw_auc)
        list_translated_auc.append(epoch_translated_auc)

    with np.printoptions(precision=4, suppress=True):
        print(np.array(list_raw_auc))
        print(np.array(list_translated_auc))

    if USE_EMA and SAVE_MODEL:
        # Load EMA parameters and evaluate once again
        for name, param in classifier.named_parameters():
            if param.requires_grad:
                param.data = ema.get(name)
        epoch_raw_auc = evaluate(classifier, val_raw_tuple)
        epoch_translated_auc = evaluate(classifier, val_translated_tuple)
        print('EMA - Raw: {:.4f}, Translated: {:.4f}'.format(epoch_raw_auc, epoch_translated_auc))
        save_model(os.path.join(OUTPUT_DIR, '{}_ema'.format(PRETRAINED_MODEL)), classifier, pretrained_config,
                   tokenizer)


if __name__ == '__main__':
    start_time = time.time()

    # Load train, validation, and pseudo-label data
    train_ids, train_strings, train_labels = get_id_text_label_from_csv(TRAIN_CSV_PATH,
                                                                        text_col='comment_text',
                                                                        sample_frac=TRAIN_SAMPLE_FRAC)
    train_lang_labels = np.repeat(np.expand_dims(LANG_MAPPING['en'], 0), len(train_labels), 0)
    train_labels = np.expand_dims(train_labels, 1) * train_lang_labels

    val_ids, val_raw_strings, val_labels = get_id_text_label_from_csv(VAL_CSV_PATH, text_col='comment_text')
    val_lang_labels = np.stack(pd.read_csv(VAL_CSV_PATH)['lang'].map(LANG_MAPPING).values, 0)
    val_labels = np.expand_dims(val_labels, 1) * val_lang_labels

    _, val_translated_strings, _ = get_id_text_label_from_csv(VAL_CSV_PATH, text_col='comment_text_en')

    pseudo_ids, pseudo_strings, pseudo_labels = get_id_text_label_from_csv(PSEUDO_CSV_PATH, text_col='content')
    pseudo_lang_labels = np.stack(pd.read_csv(PSEUDO_CSV_PATH)['lang'].map(LANG_MAPPING).values, 0)
    pseudo_labels = np.expand_dims(pseudo_labels, 1) * pseudo_lang_labels

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
        train_features = np.array(p.map(encode_partial, train_strings))
        val_raw_features = np.array(p.map(encode_partial, val_raw_strings))
        val_translated_features = np.array(p.map(encode_partial, val_translated_strings))
        pseudo_features = None
        if USE_PSEUDO:
            pseudo_features = np.array(p.map(encode_partial, pseudo_strings))

    # if USE_PSEUDO:  # so that when we switch, can just use this tuple imeddiately
    #     pseudo_features = np.concatenate([val_raw_features, pseudo_features])
    #     pseudo_labels = np.concatenate([val_labels, pseudo_labels])
    #     pseudo_ids = np.concatenate([val_ids, pseudo_ids])

    train_features = np.concatenate([train_features, val_raw_features])
    train_labels = np.concatenate([train_labels, val_labels])
    train_ids = np.concatenate([train_ids, val_ids])
    train_lang_labels = np.concatenate([train_lang_labels, val_lang_labels])

    if USE_PSEUDO:
        train_features = np.concatenate([train_features, pseudo_features])
        train_labels = np.concatenate([train_labels, pseudo_labels])
        train_ids = np.concatenate([train_ids, pseudo_ids])
        train_lang_labels = np.concatenate([train_lang_labels, pseudo_lang_labels])

    print('Train size: {}, val size: {}, pseudo size: {}'.format(len(train_ids), len(val_ids), len(pseudo_ids)))
    print('Train lang label size: {}, val size: {}, pseudo size: {}'.format(train_lang_labels.shape,
                                                                            val_lang_labels.shape,
                                                                            pseudo_lang_labels.shape))

    main_driver([train_features, train_labels, train_lang_labels, train_ids],
                [val_raw_features, val_labels, val_lang_labels, val_ids],
                [val_translated_features, val_labels, val_lang_labels, val_ids],
                [pseudo_features, pseudo_labels, pseudo_lang_labels, pseudo_ids],
                tokenizer)

    print('Elapsed time: {}'.format(time.time() - start_time))

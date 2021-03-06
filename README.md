### Introduction
PyTorch/Tensorflow model code used by the 1st place winner for the  [2020 Jigsaw Multilingual Kaggle competition](https://www.kaggle.com/c/jigsaw-multilingual-toxic-comment-classification). 
Our solution is detailed in this [Kaggle forum post](https://www.kaggle.com/c/jigsaw-multilingual-toxic-comment-classification/discussion/160862). 

This is "one-half" of the overall solution designed for and executed on local hardware. 
The other-half, created by my teammate [@rafiko1](https://www.kaggle.com/rafiko1) and designed for and executed on Kaggle TPUv3 instances, can be found at the GitHub repo: [here](https://github.com/rafiko1/jigsaw-multilingual-competition). 

### Recipe for training: 
1. Bootstrap test-set predictions (pseudo-labels) for all languages using the 
XLM-Roberta-Large multilingual model
2. Combine pseudo-labels with training data, train a monolingual/multilingual 
model, and predict test-set samples in the relevant language(s)
3. Blend predictions from the current model with the previous ensemble 
predictions - update pseudo-labels 
4. Repeat steps 2 and 3 with various pretrained monolingual and multilingual 
models 

We provide 3 sets of sample pseudo-labels (scoring on public LB: 9372, 9500, 9537) for you to test training monolingual and multilingual models against (refer to the Data and model files section below). 


You can use the provided 9372 pseudo-labels to bootstrap your training process or alternatively, run the [public Kaggle kernel](https://www.kaggle.com/xhlulu/jigsaw-tpu-xlm-roberta) (created by Kaggle user [@xhlulu](https://www.kaggle.com/xhlulu)) to generate test-set predictions/pseudo-labels using a vanilla XLM-Roberta-Large multilingual model.  


### Code
| Model | Comment |
| ----- | ------  |
|[FastText BiGRU](classifier_bigru_fasttext_tf.py) | monolingual RNN approach using non-contextualized FastText embeddings |
|[HuggingFace Transformer](classifier_baseline.py) | mono/multilingual Transformer approach |  

| Helper modules | Comment | 
| -------------- | ------- |
| [prepare_data](prepare_data.py) | Generates the prerequisite train/test/validation data necessary for training |
| [prepare_predictions](prepare_predictions.py) | Blends current run predictions with the previous ensemble |
| [preprocessor](preprocessor.py)| Includes helper functions to extract raw strings and labels from training CSVs |
| [postprocessor](postprocessor.py)| Includes helper functions to ensemble multiple CSV predictions |

### Data and model files
1. HuggingFace models are downloaded directly via API so there is no need to manually download them.
2. FastText monolingual models can be downloaded [here](https://fasttext.cc/docs/en/crawl-vectors.html). Our model code uses bin model files (e.g., cc.es.300.bin).
3. Translations of the Toxic 2018 dataset and pseudo-labels for public LB 9372, public LB 9500, public LB 9537 (used as sample inputs to training) can be found [here](https://www.kaggle.com/leecming/multilingual-toxic-comments-training-data).

### Setup
1. A [DockerFile](Dockerfile) is provided which builds against an Nvidia-provided Docker image, and installs the necessary Nvidia, system, and Python prerequisites - IMPORTANT: the Dockerfile installs an SSH server in-container with a default password (root/testdocker). 
2. Sample Docker build and run invocation: docker build -t jigsaw-multi .
&& docker run
-p 8888:22 -p 8889:8888
-v /home/leecming/PycharmProjects/jigsaw-multilingual/data:/root/data
-v /home/leecming/PycharmProjects/jigsaw-multilingual/models:/root/models
-v /home/leecming/PycharmProjects/jigsaw-multilingual/notebooks:/root/notebooks
--name jigsaw-multi
jigsaw-multi 
3. Various functions ingest and generate files - it is suggested that you mount them within container volumes to allow for smooth movement of files in & out of the container
4. With an SSH server and Jupyter notebook server within the container - it is suggested that you bind ports to enable external connections
5. Training I/O configuration: update [SETTINGS.json](SETTINGS.json) to point to locations of the 2018 training CSV, pseudo-labels CSV, and various other relevant paths.
6. To configure which languages to generate training data for, update the LANG_LIST global variable in [prepare_data.py](prepare_data.py) (accepts 1 or more of the 6 test-set languages in ISO code)
7. For Transformer model settings (including which pretrained model to use), update the global variables in [classifier_base.py](classifier_baseline.py)
8. For FastText classifier model settings (including which language model to use), update the global variables in [classifier_bigru_fasttext_tf.py](classifier_bigru_fasttext_tf.py)


### Example 1: Running a spanish monolingual Transformer model using public LB 9500 pseudo-labels 
1. We use the pretrained spanish model: mrm8488/distill-bert-base-spanish-wwm-cased-finetuned-spa-squad2-es
2. Update [SETTINGS.json](SETTINGS.json) so that PSEUDO_LABELS_PATH and other paths are updated for your setup.
3. Run prepare_data.py
4. Run classifier_baseline.py
5. Run prepare_predictions.py

A submission file should be generated at $TRAIN_DATA_DIR/curr_run_submission.csv. 
For this, we were able to generate a 9502 public LB submission. 
Due to training variability, you may get different results.
To use the predictions as pseudo-labels for another model run, note that you'll have to merge the predicted labels with test.csv. 


### Example 2: Running a spanish monolingual FastText model using public LB9537 pseudo-labels
1. We use the pretrained official FastText embeddings for spanish (cc.es.300.bin) 
2. Update [SETTINGS.json](SETTINGS.json) so that PSEUDO_LABELS_PATH and other paths are updated for your setup.
3. Run prepare_data.py
4. Run classifier_bigru_fasttext_tf.py
5. Update prepare_predictions.py so that ENSEMBLE_WEIGHT=0.8 (give less weight to predictions from this model)
6. Run prepare_predictions.py

A submission file should be generated at $TRAIN_DATA_DIR/curr_run_submission.csv. 
For this, we were able to generate a 9540 public LB submission. 
Due to training variability, you may get different results.
To use the predictions as pseudo-labels for another model run, note that you'll have to merge the predicted labels with test.csv 


### Example 3: Running a multilingual Transformer model (XLM-Roberta-Large) using public LB9372 pseudo-labels
1. We use the pretrained multilingual model: xlm-roberta-large
2. Update [SETTINGS.json](SETTINGS.json) so that PSEUDO_LABELS_PATH and other paths are updated for your setup.
3. Update [prepare_data.py](prepare_data.py) so that the LANG_LIST = ['es', 'fr', 'it', 'pt', 'ru', 'tr'] and SAMPLE_FRAC=0.1
4. Update [classifier_base.py](classifier_baseline.py) so that PRETRAINED_MODEL = 'xlm-roberta-large' and BASE_MODEL_OUTPUT_DIM=1024. You'll likely need to adjust the ACCUM_FLAG and BATCH_SIZE given the model size (ACCUM_FLAG=2 and BATCH_SIZE=24 on a single RTX Titan)
5. Run classifier_base.py
6. Update prepare_predictions.py so that ENSEMBLE_WEIGHT=0.2 (give more weight to predictions from this model)
7. Run prepare_predictions.py

A submission file should be generated at $TRAIN_DATA_DIR/curr_run_submission.csv. 
For this, we were able to generate a 9409 public LB submission. 
Due to training variability, you may get different results.
To use the predictions as pseudo-labels for another model run, note that you'll have to merge the predicted labels with test.csv
 



### Notes
1. The code and container environment were run on a fairly heavy-weight workstation (24C/48T Threadripper + 64GB RAM + 2x RTX Titans w/ 24GB GPU RAM each) and using mixed precision training + gradient accumulation. It may not be feasible to finetune larger models such as XLM-Roberta-Large on smaller-scale machines especially with entry-level Nvidia cards.
You can adjust the BATCH_SIZE and ACCUM_FOR flags in classifier_baseline.py to fit in memory but it may impact model performance. 

2. Below lists the various pretrained HuggingFace transformer models we used -

| Language | Models |
| -------- | ------ | 
| All | xlm-roberta-large |
| French | camembert-base, camembert/camembert-large, flaubert/flaubert_large_cased |
| Italian | dbmdz/bert-base-italian-xxl-cased, dbmdz/bert-base-italian-xxl-uncased, m-polignano-uniba/bert_uncased_L-12_H-768_A-12_italian_alb3rt0 |
| Portuguese | neuralmind/bert-large-portuguese-cased | 
| Russian | DeepPavlov/rubert-base-cased-conversational |
| Spanish | dccuchile/bert-base-spanish-wwm-cased, dccuchile/bert-base-spanish-wwm-uncased, mrm8488/distill-bert-base-spanish-wwm-cased-finetuned-spa-squad2-es |
| Turkish | dbmdz/bert-base-turkish-128k-cased, dbmdz/bert-base-turkish-128k-uncased, dbmdz/electra-base-turkish-cased-v0-discriminator |
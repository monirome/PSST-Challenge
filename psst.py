# -*- coding: utf-8 -*-
"""PSST.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/14mk4nXksRBsA8h03wYT9ILMHr6Xf36tk
"""

# Commented out IPython magic to ensure Python compatibility.
# %%capture
# !pip install datasets==1.13.3
# !pip install transformers==4.11.3
# !pip install torchaudio
# !pip install librosa
# !pip install jiwer

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import datasets
from datasets import Audio
import numpy as np
import torch
from packaging import version
from torch import nn

import transformers
from transformers import (
    HfArgumentParser,
    Trainer,
    TrainingArguments,
    Wav2Vec2CTCTokenizer,
    Wav2Vec2FeatureExtractor,
    Wav2Vec2ForCTC,
    Wav2Vec2Processor,
    set_seed,
)
from transformers.trainer_utils import get_last_checkpoint, is_main_process
transformers.logging.set_verbosity_info()

if version.parse(torch.__version__) >= version.parse("1.6"):
    _is_native_amp_available = True
    from torch.cuda.amp import autocast

logger = logging.getLogger(__name__)

def list_field(default=None, metadata=None):
    return field(default_factory=lambda: default, metadata=metadata)
############################################################################################################
import pandas as pd
import psstdata

# data = psstdata.load()

df_train = pd.read_csv("df_train_psst.csv")
df_test = pd.read_csv("df_test_psst.csv")

df_train["filename"]="psst-data/psst-data-2022-03-02/train/"+df_train["filename"]
df_train_prueba = df_train[:10]
df_train_prueba
df_train_prueba = df_train_prueba[["transcription", "filename"]]
df_train_prueba.columns = ["file", "audio"]
df_train_prueba

df_test["filename"]="psst-data/psst-data-2022-03-02/train/"+df_test["filename"]
df_test_prueba = df_train[:10]
df_test_prueba
df_test_prueba = df_test_prueba[["transcription", "filename"]]
df_test_prueba.columns = ["file", "audio"]
df_test_prueba

############################################################################################################
import datasets
from datasets import load_dataset, Dataset

df_train_prueba = Dataset.from_pandas(df_train_prueba)
df_test_prueba = Dataset.from_pandas(df_test_prueba)

############################################################################################################
def extract_all_chars(batch):
  all_text = " ".join(batch["file"])
  vocab = list(set(all_text))
  return {"vocab": [vocab], "all_text": [all_text]}

vocab_train = df_train_prueba.map(extract_all_chars, batched=True, batch_size=-1,
                                    keep_in_memory=True, remove_columns=df_train_prueba.column_names)

vocab_test = df_test_prueba.map(extract_all_chars, batched=True, batch_size=-1,
                                    keep_in_memory=True, remove_columns=df_test_prueba.column_names)

vocab_list = list(set(vocab_train["vocab"][0]) | set(vocab_test["vocab"][0]))
vocab_dict = {v: k for k, v in enumerate(vocab_list)}
vocab_dict["|"] = vocab_dict[" "]
del vocab_dict[" "]

vocab_dict["[UNK]"] = len(vocab_dict)
vocab_dict["[PAD]"] = len(vocab_dict)
print(len(vocab_dict))

import json
with open('vocab.json', 'w') as vocab_file:
    json.dump(vocab_dict, vocab_file)

#################################################################
# PREPARE FINETUNE ######################################################
from transformers import Wav2Vec2CTCTokenizer, Wav2Vec2FeatureExtractor, Wav2Vec2Processor
tokenizer = Wav2Vec2CTCTokenizer("./vocab.json", unk_token="[UNK]", pad_token="[PAD]", word_delimiter_token="|")
feature_extractor = Wav2Vec2FeatureExtractor(feature_size=1, sampling_rate=16000, padding_value=0.0, do_normalize=True, return_attention_mask=True)
processor = Wav2Vec2Processor(feature_extractor=feature_extractor, tokenizer=tokenizer)

#################################################################
# PREPARE AUDIOS ######################################################
import torchaudio
from datasets import Audio 

df_train_prueba = df_train_prueba.cast_column("audio", Audio(sampling_rate=16_000))
df_test_prueba = df_test_prueba.cast_column("audio", Audio(sampling_rate=16_000))

from datasets import load_metric

##############################################
# Prepare data for training ##################

def prepare_dataset(batch):
        audio = batch["audio"]

        # batched output is "un-batched"
        batch["input_values"] = processor(audio["array"], sampling_rate=audio["sampling_rate"]).input_values[0]
        batch["input_length"] = len(batch["input_values"])

        with processor.as_target_processor():
            batch["labels"] = processor(batch["file"]).input_ids
        return batch

df_train_prueba = df_train_prueba.map(prepare_dataset, remove_columns=df_train_prueba.column_names)
df_test_prueba = df_test_prueba.map(prepare_dataset, remove_columns=df_test_prueba.column_names)
print(df_train_prueba)
print(df_test_prueba)

###################################################
# Training ########################################

import torch
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

@dataclass
class DataCollatorCTCWithPadding:
    """
    Data collator that will dynamically pad the inputs received.
    Args:
        processor (:class:`~transformers.Wav2Vec2Processor`)
            The processor used for proccessing the data.
        padding (:obj:`bool`, :obj:`str` or :class:`~transformers.tokenization_utils_base.PaddingStrategy`, `optional`, defaults to :obj:`True`):
            Select a strategy to pad the returned sequences (according to the model's padding side and padding index)
            among:
            * :obj:`True` or :obj:`'longest'`: Pad to the longest sequence in the batch (or no padding if only a single
              sequence if provided).
            * :obj:`'max_length'`: Pad to a maximum length specified with the argument :obj:`max_length` or to the
              maximum acceptable input length for the model if that argument is not provided.
            * :obj:`False` or :obj:`'do_not_pad'` (default): No padding (i.e., can output a batch with sequences of
              different lengths).
        max_length (:obj:`int`, `optional`):
            Maximum length of the ``input_values`` of the returned list and optionally padding length (see above).
        max_length_labels (:obj:`int`, `optional`):
            Maximum length of the ``labels`` returned list and optionally padding length (see above).
        pad_to_multiple_of (:obj:`int`, `optional`):
            If set will pad the sequence to a multiple of the provided value.
            This is especially useful to enable the use of Tensor Cores on NVIDIA hardware with compute capability >=
            7.5 (Volta).
    """
    processor: Wav2Vec2Processor
    padding: Union[bool, str] = True
    max_length: Optional[int] = None
    max_length_labels: Optional[int] = None
    pad_to_multiple_of: Optional[int] = None
    pad_to_multiple_of_labels: Optional[int] = None
    def __call__(self, features: List[Dict[str, Union[List[int], torch.Tensor]]]) -> Dict[str, torch.Tensor]:
        # split inputs and labels since they have to be of different lenghts and need
        # different padding methods
        input_features = [{"input_values": feature["input_values"]} for feature in features]
        label_features = [{"input_ids": feature["labels"]} for feature in features]
        batch = self.processor.pad(
            input_features,
            padding=self.padding,
            max_length=self.max_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors="pt",
        )
        with self.processor.as_target_processor():
            labels_batch = self.processor.pad(
                label_features,
                padding=self.padding,
                max_length=self.max_length_labels,
                pad_to_multiple_of=self.pad_to_multiple_of_labels,
                return_tensors="pt",
            )
        # replace padding with -100 to ignore loss correctly
        labels = labels_batch["input_ids"].masked_fill(labels_batch.attention_mask.ne(1), -100)
        batch["labels"] = labels
        return batch

data_collator = DataCollatorCTCWithPadding(processor=processor, padding=True)
wer_metric = load_metric("wer")

def compute_metrics(pred):
    pred_logits = pred.predictions
    pred_ids = np.argmax(pred_logits, axis=-1)
    pred.label_ids[pred.label_ids == -100] = processor.tokenizer.pad_token_id
    pred_str = processor.batch_decode(pred_ids)
    # we do not want to group tokens when computing the metrics
    label_str = processor.batch_decode(pred.label_ids, group_tokens=False)
    wer = wer_metric.compute(predictions=pred_str, references=label_str)
    return {"wer": wer}


from transformers import Wav2Vec2ForCTC, TrainingArguments, Trainer

model = Wav2Vec2ForCTC.from_pretrained(
    "facebook/wav2vec2-large-xlsr-53",
    attention_dropout=0.1,
    activation_dropout=0.05,
    hidden_dropout=0.05,
    final_dropout=0.1,
    feat_proj_dropout=0.05,
    mask_time_prob=0.05,
    layerdrop=0.04,
    gradient_checkpointing=True,
    ctc_loss_reduction="mean",
    pad_token_id=processor.tokenizer.pad_token_id,
    vocab_size=len(processor.tokenizer)
)

model.freeze_feature_extractor()

training_args = TrainingArguments(
  output_dir="wav2vec2-large-xlsr-demo",
#   result_dir = "/results",
  group_by_length=True,
  per_device_train_batch_size=16,
  gradient_accumulation_steps=2,
  evaluation_strategy="steps",
  num_train_epochs=200,
  seed=6,
#   batch_size=8,
  fp16=True,
  save_steps=500, 
  eval_steps=100,
  logging_steps=400,
  learning_rate=2e-4,
  warmup_steps=500,
  save_total_limit=2,
)

trainer = Trainer(
    model=model,
    data_collator=data_collator,
    args=training_args,
    compute_metrics=compute_metrics,
    train_dataset=df_train_prueba,
    eval_dataset=df_test_prueba,
    tokenizer=processor.feature_extractor,
)

#
print("# START TRAINING")
trainer.train()



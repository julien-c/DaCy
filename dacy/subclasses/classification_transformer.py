from typing import List, Callable, Iterable, Iterator, Optional, Dict, Union
from pathlib import Path
from spacy import language

from spacy.language import Language
from spacy import util
from spacy.pipeline.pipe import deserialize_config
from spacy.tokens import Doc
from spacy.vocab import Vocab

from spacy_transformers import Transformer
from spacy_transformers import annotation_setters
from spacy_transformers.layers.transformer_model import forward, set_pytorch_transformer
from spacy_transformers.data_classes import (
    FullTransformerBatch,
    TransformerData,
    WordpieceBatch,
)
from spacy_transformers.annotation_setters import null_annotation_setter
from spacy_transformers.util import registry, huggingface_tokenize, find_last_hidden
from spacy_transformers.align import get_alignment
from spacy_transformers.truncate import truncate_oversize_splits

from thinc.api import (
    get_current_ops,
    CupyOps,
    Model,
    PyTorchWrapper,
    Config,
)
from thinc.types import ArgsKwargs

import torch
import numpy as np

from transformers import AutoTokenizer, AutoModelForSequenceClassification

from ..utils import softmax

DEFAULT_CONFIG_STR = """
[classification_transformer]
max_batch_items = 4096
doc_extension_attribute = "clf_trf_data"

[classification_transformer.set_extra_annotations]
@annotation_setters = "spacy-transformers.null_annotation_setter.v1"

[classification_transformer.model]
@architectures = "dacy.ClassificationTransformerModel.v1"
name = "roberta-base"
tokenizer_config = {"use_fast": true}
num_labels = 2

[classification_transformer.model.get_spans]
@span_getters = "spacy-transformers.strided_spans.v1"
window = 128
stride = 96
"""

DEFAULT_CONFIG = Config().from_str(DEFAULT_CONFIG_STR)


@Language.factory(
    "classification_transformer",
    default_config=DEFAULT_CONFIG["classification_transformer"],
)
def make_classification_transformer(
    nlp: Language,
    name: str,
    model: Model[List[Doc], FullTransformerBatch],
    set_extra_annotations: Callable[[List[Doc], FullTransformerBatch], None],
    max_batch_items: int,
    doc_extension_attribute: str,
):
    """
    Construct a Transformer component, which lets you plug a model from the
    Huggingface transformers library into spaCy so you can use it in your
    pipeline. One or more subsequent spaCy components can use the transformer
    outputs as features in its model, with gradients backpropagated to the single
    shared weights.

    Args:
        nlp (Language): a spacy text-processing pipeline
        name (str): the desired name of the component
        model (Model[List[Doc], FullTransformerBatch]):  A thinc Model object wrapping
        the transformer. Usually you will want to use the TransformerModel
        layer for this.
        set_extra_annotations (Callable[[List[Doc], FullTransformerBatch], None]): A
        callback to set additional information onto the batch of `Doc` objects.
        The doc._.clf_trf_data attribute is set prior to calling the callback.
        By default, no additional annotations are set.
        doc_extension_attribute (str): The desired doc extension attribute

    Returns:
        Your component which is a ClassificationTransformer
    """

    return ClassificationTransformer(
        nlp.vocab,
        model,
        set_extra_annotations,
        max_batch_items=max_batch_items,
        name=name,
        doc_extension_attribute=doc_extension_attribute,
    )



@registry.architectures.register("dacy.ClassificationTransformerModel.v1")
def ClassificationTransformerModel(
    name: str, get_spans: Callable, tokenizer_config: dict, num_labels: int
) -> Model[List[Doc], FullTransformerBatch]:
    """
    Args:
        get_spans (Callable): A function to extract spans from the batch of Doc objects.
    This is used to manage long documents, by cutting them into smaller
    sequences before running the transformer. The spans are allowed to
    overlap, and you can also omit sections of the Doc if they are not
    relevant.
        tokenizer_config (dict): Settings to pass to the transformers tokenizer.
        num_labels (int): [description]
    """

    return Model(
        "classification_transformer",
        forward,
        init=init,
        layers=[],
        dims={"nO": None},
        attrs={
            "tokenizer": None,
            "get_spans": get_spans,
            "name": name,
            "tokenizer_config": tokenizer_config,
            "num_labels": num_labels,
            "set_transformer": set_pytorch_transformer,
            "has_transformer": False,
            "flush_cache_chance": 0.0,
        },
    )


class ClassificationTransformer(Transformer):
    """
    A wrapper class for the SpaCy transformer component which is intended for wrapping a huggingface transformer trained for sequence classification
    """

    def __init__(
        self,
        vocab: Vocab,
        model: Model[List[Doc], FullTransformerBatch],
        set_extra_annotations: Callable = null_annotation_setter,
        *,
        name: str = "classification_transformer",
        max_batch_items: int = 128 * 32,  # Max size of padded batch
        doc_extension_attribute,
    ):
        """
        Args:
            vocab (Vocab): a spacy vocabulary
            model (Model[List[Doc], FullTransformerBatch]): A thinc Model object
            set_extra_annotations (Callable, optional): Defaults to null_annotation_setter.
            name (str, optional): The name of the compenent. Defaults to "classification_transformer".
            max_batch_items (int, optional): Maximum batch size. Defaults to 128*32.
        """
        super().__init__(
            vocab=vocab,
            model=model,
            set_extra_annotations=set_extra_annotations,
            name=name,
            max_batch_items=max_batch_items,
        )
        install_extensions(doc_extension_attribute)
        self.doc_extension_attribute = doc_extension_attribute

        """Load the pipe from disk.
        num_labels (int): Number of labels of the model
        path (str / Path): Path to a directory.
        exclude (Iterable[str]): String names of serialization fields to exclude.
        RETURNS (Transformer): The loaded object.
        DOCS: https://spacy.io/api/transformer#from_disk
        """
    def from_disk(
        self,
        path: Union[str, Path],
        *,
        num_labels: int = 3,
        exclude: Iterable[str] = tuple(),
    ) -> "ClassificationTransformer":
        """
        Load the pipe from disk. For more see https://spacy.io/api/transformer#from_disk

        Args:
            path (str): path to the model
            num_labels (int): number of labels of the model
            exclude (Iterable[str]): String names of serialization fields to exclude.

        Returns:
            ClassificationTransformer: The loaded object.
        """

        def load_model(p):
            p = Path(p).absolute()
            tokenizer, transformer = huggingface_classification_from_pretrained(
                p, self.model.attrs["tokenizer_config"], num_labels=num_labels
            )
            self.model.attrs["tokenizer"] = tokenizer
            self.model.attrs["set_transformer"](self.model, transformer)

        deserialize = {
            "vocab": self.vocab.from_disk,
            "cfg": lambda p: self.cfg.update(deserialize_config(p)),
            "model": load_model,
        }
        util.from_disk(path, deserialize, exclude)
        return self

    def set_annotations(
        self, docs: Iterable[Doc], predictions: FullTransformerBatch
    ) -> None:
        """
        Assign the extracted features to the Doc objects. By default, the
        TransformerData object is written to the doc._.trf_data attribute. Your
        set_extra_annotations callback is then called, if provided. for more see:  
        https://spacy.io/api/pipe#set_annotations

        Args:
            docs (Iterable[Doc]): The documents to modify.
            predictions: (FullTransformerBatch): A batch of activations.
        """
        doc_data = list(predictions.doc_data)
        for doc, data in zip(docs, doc_data):
            setattr(doc._, self.doc_extension_attribute, data)
        self.set_extra_annotations(docs, predictions)


def init(model: Model, X=None, Y=None):
    if model.attrs["has_transformer"]:
        return
    name = model.attrs["name"]
    tok_cfg = model.attrs["tokenizer_config"]
    num_labels = model.attrs["num_labels"]
    tokenizer, transformer = huggingface_classification_from_pretrained(
        name, tok_cfg, num_labels
    )
    model.attrs["tokenizer"] = tokenizer
    model.attrs["set_transformer"](model, transformer)
    # Call the model with a batch of inputs to infer the width
    texts = ["hello world", "foo bar"]
    token_data = huggingface_tokenize(model.attrs["tokenizer"], texts)
    wordpieces = WordpieceBatch.from_batch_encoding(token_data)
    model.layers[0].initialize(X=wordpieces)
    tensors = model.layers[0].predict(wordpieces)


def huggingface_classification_from_pretrained(
    source: Union[Path, str], config: Dict, num_labels: int
):
    """
    Create a Huggingface transformer model from pretrained weights. Will
    download the model if it is not already downloaded.
    
    Args:
        source (Union[str, Path]): The name of the model or a path to it, such as
            'bert-base-cased'.
        config (dict): Settings to pass to the tokenizer.
    """
    if hasattr(source, "absolute"):
        str_path = str(source.absolute())
    else:
        str_path = source
    tokenizer = AutoTokenizer.from_pretrained(str_path, **config)
    transformer = AutoModelForSequenceClassification.from_pretrained(
        str_path, num_labels=num_labels
    )
    ops = get_current_ops()
    if isinstance(ops, CupyOps):
        transformer.cuda()
    return tokenizer, transformer


def make_classification_getter(category, labels, doc_extension):
    def prop_getter(doc) -> dict:
        trf_data = getattr(doc._, doc_extension)
        return {
            "prop": softmax(trf_data.tensors[0][0]).round(decimals=3),
            "labels": labels,
        }

    def label_getter(doc) -> str:
        prop = getattr(doc._, f"{category}_prop")
        return labels[np.argmax(prop["prop"])]

    return prop_getter, label_getter


def install_extensions(doc_extension_attribute) -> None:
    if not Doc.has_extension(doc_extension_attribute):
        Doc.set_extension(doc_extension_attribute, default=None)


def install_classification_extensions(
    category: str,
    labels: list,
    doc_extension: str,
    force: bool,
):
    prop_getter, label_getter = make_classification_getter(
        category, labels, doc_extension
    )
    Doc.set_extension(f"{category}_prop", getter=prop_getter, force=force)
    Doc.set_extension(category, getter=label_getter, force=force)


def add_huggingface_model(
    nlp: Language,
    download_name: str,
    doc_extension: str,
    model_name: str,
    category: str,
    labels: list,
    verbose: bool = True,
    force_extension: bool = False,
) -> Language:
    """
    Adds a Huggingface sequence classification model to the pipeline

    Args:
        nlp (Language): a SpaCy text-processing pipeline
        download_name (str): the name of the model on huggingface model hub
        doc_extension (str): the doc extension on which the transformer data should be stored. This includes the forward pass, wordpieces and more.
        model_name (str):  What you want your model to be called in the nlp pipeline
        category (str): The category of the output. This is the label which is used to extract from the model. E.g. "sentiment" would allow you to extract the sentiment from doc._.sentiment
        labels (list): The labels of the model
        verbose (bool, optional): Toggles the verbosity of the download. Defaults to True.
        force_extension (bool, optional): Set the extension to the doc regardless of whether it already exists. Defaults to False.

    Returns:
        Language: your text processing pipeline with the transformer model included

    Example:
        add_huggingface_model(nlp, download_name="pin/senda", doc_extension="senda_trf_data", model_name="senda",
                                category="polarity", labels=["negative", "neutral", "positive"])
    """
    config = {
        "doc_extension_attribute": doc_extension,
        "model": {
            "@architectures": "dacy.ClassificationTransformerModel.v1",
            "name": download_name,
            "num_labels": len(labels),
        },
    }

    install_classification_extensions(
        category=category,
        labels=labels,
        doc_extension=doc_extension,
        force=force_extension,
    )

    transformer = nlp.add_pipe(
        "classification_transformer", name=model_name, config=config
    )
    transformer.model.initialize()
    return nlp

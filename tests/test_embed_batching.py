"""The encoder keeps its RAM.

Measured on an M1 with the bundled int8 model before these changes: one
`embed_passages` of 64 full 448-token windows took a process from 118 MB to
1.5 GB resident, a second identical call to 3 GB, and neither was given back,
because onnxruntime's arena keeps everything it grows to. Six MCP servers on
one 8 GB laptop each did that on their own. The fixes are an encoder with no
arena, batches sized in padded tokens rather than in texts, and a vault open
that checks the model pin from the files instead of building the encoder.
"""
import subprocess
import sys

import numpy as np
import pytest

from compartment import embed
from compartment.embed import (BATCH_TOKENS, BUNDLED_HASHES, DEFAULT_MODEL,
                               Embedder, model_info)
from compartment.vault import Vault

from conftest import PASS


@pytest.fixture(scope="module")
def emb():
    return Embedder()


def test_the_inference_arena_is_off(emb):
    """The setting the measurements above turn on. Without it the process
    keeps the high-water mark of its largest batch forever."""
    assert emb.sess.get_session_options().enable_cpu_mem_arena is False


def test_batches_respect_the_token_budget_and_the_count():
    ids = [[0] * n for n in (5, 500, 40, 448, 448, 448, 12, 448, 3, 448, 448)]
    order = sorted(range(len(ids)), key=lambda i: len(ids[i]))
    groups = Embedder._batches(order, ids, batch=64)
    assert sorted(i for g in groups for i in g) == list(range(len(ids)))
    for g in groups:
        longest = max(len(ids[i]) for i in g)
        assert len(g) * longest <= BATCH_TOKENS
    # nine full windows do not fit under the budget together
    assert len(groups) >= 2
    assert all(len(g) <= 2 for g in Embedder._batches(order, ids, batch=2))


def test_one_text_over_the_budget_still_gets_a_batch_of_its_own():
    assert Embedder._batches([0], [[0] * (BATCH_TOKENS * 2)], batch=64) == [[0]]


def test_passages_come_back_in_the_order_they_were_given(emb):
    """Packing sorts by length internally; the caller must never see that.

    The vectors are compared by cosine rather than for equality because
    the int8 model quantizes each activation tensor dynamically, so a text's
    batch-mates set the scale it is quantized at: measured, a short text
    embedded beside long ones lands at cosine 0.993-0.998 to itself embedded
    alone, and a query's score against it moves by about 0.001. That was
    equally true of the previous batching. A wrong row order would show as
    a cosine near zero, which is what this guards against."""
    texts = ["zebra crossing " * 300, "apple", "the one in the middle " * 20, "b"]
    out = emb.embed_passages(texts)
    assert out.shape == (4, emb.dim)
    for i, t in enumerate(texts):
        alone = emb.embed_passages([t])[0]
        assert float(np.dot(out[i], alone)) > 0.99
    # and the rows are distinct texts, not one text repeated
    assert float(np.dot(out[0], out[1])) < 0.95


def test_padding_is_the_tokenizers_padding(emb):
    """Id 0, mask 0: what `enable_padding()` produced before the encoder
    took padding over. The residual difference (see the order test above)
    is the model's dynamic quantization, the same as it always was."""
    ids = emb._encode(["one", "a somewhat longer second text"])
    assert len(ids[0]) < len(ids[1])
    vec_batch = emb._infer(ids)
    vec_alone = emb._infer([ids[0]])
    assert float(np.dot(vec_batch[0], vec_alone[0])) > 0.99


def test_empty_input_is_an_empty_matrix(emb):
    assert emb.embed_passages([]).shape == (0, emb.dim)


def test_vectors_are_unit_length(emb):
    v = emb.embed_passages(["a", "b c d", "e " * 100])
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-4)


def test_model_info_is_the_pin_and_the_dimension():
    info = model_info(DEFAULT_MODEL)
    assert info.sha256 == BUNDLED_HASHES["model_quantized.onnx"]
    assert info.dim == embed.DEFAULT_DIM
    assert info.prefix_query == embed.BGE_QUERY_INSTRUCTION
    assert info.onnx.is_file() and info.tokenizer.is_file()


def test_model_info_loads_no_runtime():
    """The whole point: a fresh interpreter can answer the pin check without
    importing onnxruntime or tokenizers."""
    code = ("import sys; from compartment.embed import model_info; "
            "model_info(); print('onnxruntime' in sys.modules, "
            "'tokenizers' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120, check=True)
    assert out.stdout.split() == ["False", "False"]


def test_the_embedder_agrees_with_model_info(emb):
    info = model_info(DEFAULT_MODEL)
    assert (emb.model_sha256, emb.dim) == (info.sha256, info.dim)


def test_unlock_does_not_build_the_encoder(vault_path, monkeypatch):
    """Opening a vault checks the pin from the files. The encoder is built by
    the first search, in the process that searches."""
    Vault.create(vault_path, PASS, creator="test").lock()
    built = []
    real_init = Embedder.__init__

    def counting(self, *a, **k):
        built.append(1)
        real_init(self, *a, **k)

    monkeypatch.setattr(Embedder, "__init__", counting)
    v = Vault.unlock(vault_path, passphrase=PASS)
    assert built == []
    assert v.status()["embedder"]["mode"] == "not loaded"
    v.search("anything at all", caller="test")
    assert built == [1]
    assert v.status()["embedder"]["mode"] == "in-process"


def test_a_vault_pinned_to_another_model_is_still_refused(vault, vault_path):
    """The check moved from the encoder to the files; it did not get weaker."""
    vault.db.set_meta("model_sha256", "deadbeef" * 8)
    vault.save()
    vault.lock()
    with pytest.raises(Exception, match="does not match the model"):
        Vault.unlock(vault_path, passphrase=PASS)

"""Unit tests for OCRService pure helpers (cv2/numpy only — no paddle/GPU)."""
import numpy as np

from app import OCRService


def test_normalize_fullwidth():
    assert OCRService._normalize_fullwidth("Receipt #：1") == "Receipt #:1"
    assert OCRService._normalize_fullwidth("ＡＢ１２") == "AB12"
    assert OCRService._normalize_fullwidth("a　b") == "a b"          # ideographic space
    assert OCRService._normalize_fullwidth("normal") == "normal"


def test_is_noise():
    assert OCRService._is_noise("+++***+*") is True
    assert OCRService._is_noise("---") is False                          # page break, kept
    assert OCRService._is_noise("abc") is False
    assert OCRService._is_noise("$5.00") is False
    assert OCRService._is_noise("**") is False                           # < 3 chars


def test_strip_noise_lines():
    out = OCRService._strip_noise_lines("Hello\n+++***\n\n---\nWorld")
    lines = out.split("\n")
    assert "Hello" in lines and "World" in lines
    assert "+++***" not in out
    assert "---" in lines                                                # page break kept
    assert "" in lines                                                   # blank kept


def test_normalize_device():
    assert OCRService._normalize_device("gpu") == "gpu"
    assert OCRService._normalize_device("GPU:0") == "gpu"
    assert OCRService._normalize_device("cuda") == "gpu"
    assert OCRService._normalize_device("cpu") == "cpu"
    assert OCRService._normalize_device("") == "gpu"
    assert OCRService._normalize_device("auto") == "gpu"
    assert OCRService._normalize_device("bogus") == "cpu"


def test_resolve_device_falls_back(monkeypatch):
    svc = OCRService(device="gpu")
    monkeypatch.setattr(OCRService, "gpu_available", staticmethod(lambda: False))
    eff, note = svc._resolve_device("gpu")
    assert eff == "cpu" and note                                          # note explains fallback
    eff2, note2 = svc._resolve_device("cpu")
    assert eff2 == "cpu" and note2 is None


def test_resolve_device_gpu_available(monkeypatch):
    svc = OCRService(device="gpu")
    monkeypatch.setattr(OCRService, "gpu_available", staticmethod(lambda: True))
    eff, note = svc._resolve_device(None)                                 # default gpu
    assert eff == "gpu" and note is None


def test_enhance_image_upscales_small():
    img = np.full((50, 80, 3), 127, dtype=np.uint8)
    out = OCRService._enhance_image(img)
    assert out.ndim == 3 and out.shape[2] == 3
    assert out.shape[:2] == (100, 160)                                   # small -> 2x


def test_enhance_image_binarize():
    img = np.full((2000, 100, 3), 127, dtype=np.uint8)                   # tall -> not upscaled
    out = OCRService._enhance_image(img, binarize=True)
    assert out.shape[:2] == (2000, 100)
    assert set(np.unique(out)).issubset({0, 255})                        # binary output


def test_poly_to_bbox():
    bbox = OCRService._poly_to_bbox([(10, 20), (30, 20), (30, 40), (10, 40)])
    assert bbox == (10.0, 20.0, 30.0, 40.0)

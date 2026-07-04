"""
Long-running Cellpose segmentation service.

Loads the (expensive) Cellpose models ONCE at startup, then serves many
segmentation requests over a local HTTP endpoint. Designed to be launched as a
child process by a Java or Python parent; it shuts itself down automatically
when that parent goes away.

Protocol (HTTP over 127.0.0.1, JSON bodies):

    GET  /health   -> {"status": "ok", "ready": true}
    POST /segment  -> body. Each image may be given as a PATH or as raw BYTES:
        {
          # --- nuclei channel: supply ONE of these ---
          "nuclei_file":       "/abs/path/nuc.tif",   # path on the server, OR
          "nuclei_data":       "<base64 file bytes>", # raw bytes (any FS)
          "nuclei_suffix":     ".tif",                # optional, for *_data
          "nuclei_diameter":   30,
          # --- cytosol channel: same options ---
          "cytosol_file":      "/abs/path/cyto.tif",
          "cytosol_data":      "<base64 file bytes>",
          "cytosol_suffix":    ".tif",
          "cytosol_diameter":  60,
          # --- output ---
          "output_prefix":     "sample1",
          "output_folder":     "/abs/path/out",  # optional: also persist to disk
        }
        -> {
             "status": "ok",
             "output_prefix": "sample1",
             "elapsed_s": 1.23,
             "outputs": [                          # every file the run produced
               {"name": "sample1_mask.tif", "data": "<base64>"},
               ...
             ]
           }
        ("nuclei"/"cytosol" without a suffix are still accepted as path aliases.)
    POST /shutdown -> {"status": "stopping"}   (then the process exits)

On startup it prints exactly one line to stdout:

    CELLPOSE_SERVICE_PORT=<port>

so the launcher can discover which port the OS assigned (start with --port 0).
All human/log output goes to stderr so it never corrupts that handshake.

Shutdown happens on ANY of:
  * the parent closing our stdin pipe (EOF)  -> covers parent crash/exit
  * SIGTERM / SIGINT
  * POST /shutdown
"""

import argparse
import base64
import glob
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(
    format="%(levelname)s: %(message)s",
    level=logging.INFO,
    stream=sys.stderr,  # keep stdout clean for the PORT handshake
)
logger = logging.getLogger("cellpose-service")


class AnalyzerInterface(ABC):

    @abstractmethod
    def _run(self, nuclei_path: Path, cytosol_path: Path, nuc_diameter: int, cell_diameter: int,
             out_folder: Path, prefix: str) -> None:
        pass


class Analyzer(AnalyzerInterface):
    """Owns the loaded models. Load once, reuse for every request."""

    def __init__(self, gpu: bool):
        self.gpu = gpu
        self._lock = threading.Lock()  # GPU/model work is serialized
        self._load_models()

    def _load_models(self):
        # Import here so the service file stays importable without the heavy deps.
        import cellpose_segmentation  # noqa: F401  (kept for parity w/ your code)
        from cellpose import models, version as cellpose_version
        self.cellpose_version = cellpose_version

        t0 = time.time()
        logger.info("Loading Cellpose models (gpu=%s)...", self.gpu)
        self.model_nuc = models.CellposeModel(gpu=self.gpu, model_type="nuclei")
        self.model_cyto = models.CellposeModel(gpu=self.gpu, model_type="cyto3")
        self._segment_impl = cellpose_segmentation.segment
        logger.info("Models loaded in %.1fs. Ready.", time.time() - t0)

    def segment(self, req: dict) -> dict:
        """Handle one request. Inputs may be paths or base64 bytes; outputs are
        collected from a private temp dir and (optionally) returned + persisted.
        """
        output_prefix = req["output_prefix"]
        nuc_diameter = int(req.get("nuclei_diameter"))
        cell_diameter = int(req.get("cytosol_diameter"))

        tmp_inputs: list[str] = []
        work_out = Path(tempfile.mkdtemp(prefix="cellpose_out_"))
        t0 = time.time()
        try:
            nuclei_path = _materialize_input(req, "nuclei", tmp_inputs)
            cytosol_path = _materialize_input(req, "cytosol", tmp_inputs)

            # Serialize actual inference; the model objects are shared state.
            tempprefix = "temp_"
            with self._lock:
                self._run(nuclei_path, cytosol_path, nuc_diameter, cell_diameter,
                          work_out, tempprefix)

            output = _collect_output(work_out, tempprefix)

            elapsed = time.time() - t0
            logger.info("Segmented '%s' in %.2fs",
                        output_prefix, elapsed)
            result = {
                "status": "ok",
                "output_prefix": output_prefix,
                "elapsed_s": round(elapsed, 3),
                "output_data": output[1],
                "output_image_type": output[0],
                "output_type": output[2],
                "version": self.cellpose_version,
            }
            return result
        finally:
            shutil.rmtree(work_out, ignore_errors=True)
            for p in tmp_inputs:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _run(self, nuclei_path, cytosol_path, nuc_diameter, cell_diameter,
             out_folder, prefix) -> None:
        """Read the two images and run the real Cellpose segmentation."""
        import image_utils

        nuclei_img = image_utils.read_grayscale_image(str(nuclei_path))
        cyto_img1 = image_utils.read_grayscale_image(str(cytosol_path))
        self._segment_impl(
            model_nuc=self.model_nuc,
            model_cyto=self.model_cyto,
            nuclei_img=nuclei_img,
            cyto_img1=cyto_img1,
            cyto_img2=None,
            nuc_diameter=nuc_diameter,
            cell_diameter=cell_diameter,
            output_folder=str(out_folder),
            output_prefix=prefix,
        )


class StubAnalyzer(AnalyzerInterface):
    """Testing analyzer with no torch/cellpose. Writes a fake mask instead so the
    base64 round-trip (data in, image out) can be exercised without the models."""

    def __init__(self, gpu: bool):
        self.gpu = gpu
        self._lock = threading.Lock()
        logger.info("Loading STUB analyzer (no real model).")

    def _run(self, nuclei_path, cytosol_path, nuc_diameter, cell_diameter,
             out_folder, prefix):
        time.sleep(0.05)  # pretend to do work
        marker = (f"nuclei={os.path.basename(nuclei_path)} "
                  f"cytosol={os.path.basename(cytosol_path)} "
                  f"nd={nuc_diameter} cd={cell_diameter}").encode()
        with open(os.path.join(out_folder, prefix + "mask.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + marker)   # not a real PNG, just bytes


def _materialize_input(req: dict, channel: str, tmp_inputs: list) -> Path:
    """Resolve one image channel to a filesystem path.

    Accepts, in priority order:
      <channel>_data   base64-encoded file bytes  -> written to a temp file
      <channel>_file   a path on the server's filesystem
    Optional <channel>_suffix (default '.tif') sets the temp file extension so
    image readers that dispatch on extension (e.g. TIFF vs PNG) behave correctly.
    """
    data_key, file_key, suffix_key = f"{channel}_data", f"{channel}_file", f"{channel}_suffix"

    if req.get(data_key):
        raw = base64.b64decode(req[data_key])
        suffix = req.get(suffix_key, ".tif")
        fd, tmp = tempfile.mkstemp(prefix=f"{channel}_", suffix=suffix)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        tmp_inputs.append(tmp)
        return Path(tmp)

    path: str = req.get(file_key)
    if not path:
        raise KeyError(f"provide '{file_key}' (path) or '{data_key}' (base64)")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return Path(path)


def _collect_output(work_out: Path, tempprefix: str):
    """Gather every file the segmentation wrote into work_out. Optionally copy
    each into persist_folder, and optionally base64-encode it into the response."""
    produced = sorted(p for p in glob.glob(os.path.join(work_out, "*"))
                      if os.path.isfile(p))
    for p in produced:
        name = os.path.basename(p)
        namename = name.replace(tempprefix, "").replace(".png", "")
        if namename == "cell_mask":
            with open(p, "rb") as fh:
                return "png", base64.b64encode(fh.read()).decode("ascii"), namename
    return None

def _collect_outputs(work_out: Path, tempprefix: str):
    """Gather every file the segmentation wrote into work_out. Optionally copy
    each into persist_folder, and optionally base64-encode it into the response."""
    outputs = []
    produced = sorted(p for p in glob.glob(os.path.join(work_out, "*"))
                      if os.path.isfile(p))
    for p in produced:
        name = os.path.basename(p)
        entry = {
            "name": name.replace(tempprefix, "").replace(".png", ""),
            "suffix": ".png"
        }
        with open(p, "rb") as fh:
            entry["data"] = base64.b64encode(fh.read()).decode("ascii")
        outputs.append(entry)
    return outputs


def make_handler(analyzer: Analyzer, stop_event: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        # Silence the default per-request stderr spam; we log what we care about.
        def log_message(self, *args):
            pass

        def _send(self, code: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok", "ready": True})
            else:
                self._send(404, {"status": "error", "message": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"

            if self.path == "/shutdown":
                self._send(200, {"status": "stopping"})
                stop_event.set()
                return

            if self.path == "/segment":
                try:
                    req = json.loads(raw or b"{}")
                    result = analyzer.segment(req)
                    self._send(200, result)
                except KeyError as e:
                    self._send(400, {"status": "error",
                                     "message": f"missing field: {e}"})
                except Exception as e:  # noqa: BLE001
                    logger.exception("segment failed")
                    self._send(500, {"status": "error", "message": str(e)})
                return

            self._send(404, {"status": "error", "message": "not found"})

    return Handler


def watch_stdin(stop_event: threading.Event):
    """Block on stdin; when the parent closes the pipe we get EOF -> shut down.

    This is the key mechanism that ties our lifetime to the launcher's: as long
    as the parent holds the write end of our stdin open, read() blocks. The
    instant the parent process dies (cleanly or by crashing), the OS closes the
    pipe and read() returns '' -> we stop. Works on Linux, macOS and Windows.
    """
    try:
        while not stop_event.is_set():
            line = sys.stdin.readline()
            if line == "":  # EOF
                logger.info("stdin closed (parent gone) -> shutting down.")
                break
            # Optional: allow "shutdown" over stdin too.
            if line.strip().lower() == "shutdown":
                break
    except Exception:  # noqa: BLE001
        pass
    finally:
        stop_event.set()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0,
                        help="0 = let the OS pick a free port (recommended)")
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--stub", action="store_true",
                        help="run without torch/cellpose (for testing plumbing)")
    args = parser.parse_args()

    stop_event = threading.Event()

    analyzer = (StubAnalyzer if args.stub else Analyzer)(gpu=args.gpu)

    httpd = ThreadingHTTPServer((args.host, args.port),
                                make_handler(analyzer, stop_event))
    port = httpd.server_address[1]

    # The handshake line the launcher reads. Must be the ONLY thing on stdout.
    print(f"CELLPOSE_SERVICE_PORT={port}", flush=True)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop_event.set())

    threading.Thread(target=watch_stdin, args=(stop_event,), daemon=True).start()
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.25},
                     daemon=True).start()

    logger.info("Serving on %s:%d", args.host, port)
    stop_event.wait()
    logger.info("Stopping server.")
    httpd.shutdown()
    httpd.server_close()


if __name__ == "__main__":
    main()

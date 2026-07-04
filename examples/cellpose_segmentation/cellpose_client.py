"""
Python client for the Cellpose service.

Usage:

    from cellpose_client import CellposeService

    with CellposeService(gpu=False) as svc:      # starts the subprocess
        svc.segment(
            nuclei="/data/nuc.tif", nuclei_diameter=30,
            cytosol="/data/cyto.tif", cytosol_diameter=60,
            output_folder="/data/out", output_prefix="sample1",
        )
        svc.segment(... another image ...)
    # leaving the 'with' block (or process exit) stops the service

The service is a child process. We keep its stdin open; closing it (which
happens automatically when this process dies) tells the service to shut down.
"""

import atexit
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Dict
from urllib.error import HTTPError


class CellposeService:
    def __init__(self, gpu: bool = False, python_exe: str = None,
                 service_script: str = None, stub: bool = False,
                 startup_timeout: float = 300.0):
        self.python_exe = python_exe or sys.executable
        self.service_script = service_script or str(
            Path(__file__).with_name("cellpose_service.py"))
        self.gpu = gpu
        self.stub = stub
        self.startup_timeout = startup_timeout
        self.proc = None
        self.port = None
        self.base_url = None

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        if self.proc is not None:
            return self
        cmd = [self.python_exe, self.service_script, "--port", "0"]
        if self.gpu:
            cmd.append("--gpu")
        if self.stub:
            cmd.append("--stub")

        # stdin=PIPE: we hold the write end. When we die, it closes -> service
        # sees EOF and shuts down. stdout=PIPE: read the port handshake line.
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,          # let the service's logs flow to our stderr
            text=True,
            bufsize=1,
        )
        atexit.register(self.stop)

        # Read the single handshake line: CELLPOSE_SERVICE_PORT=<n>
        import time
        deadline = time.time() + self.startup_timeout
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError("service exited during startup")
            line = self.proc.stdout.readline()
            if line.startswith("CELLPOSE_SERVICE_PORT="):
                self.port = int(line.strip().split("=", 1)[1])
                break
            if time.time() > deadline:
                self.stop()
                raise TimeoutError("service did not report a port in time")
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._wait_ready(deadline)
        return self

    def _wait_ready(self, deadline):
        import time
        while time.time() < deadline:
            try:
                if self._get("/health").get("ready"):
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise TimeoutError("service never became ready")

    def stop(self):
        if self.proc is None:
            return
        try:
            # Closing stdin is the graceful signal; then wait, then force.
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---- requests --------------------------------------------------------
    def _get(self, path):
        with urllib.request.urlopen(self.base_url + path, timeout=10) as r:
            return json.loads(r.read())

    def _post(self, path, payload, timeout=600):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def segment(self, output_prefix, *,
                nuclei_file=None,
                cytosol_file=None,
                nuclei_diameter=None, cytosol_diameter=None,
                save_to=None, timeout=600, send_data=False):
        """Segment one image pair.

        For each channel give EITHER a path (``nuclei_file=``) or raw bytes
        (``nuclei_data=b"..."``); bytes are base64-encoded for you.

        If ``save_to`` is given, any images the service returns are written there
        and their local paths are added to each output entry as ``"path"``.
        """
        payload = {
            "output_prefix": output_prefix,
            "nuclei_diameter": nuclei_diameter,
            "cytosol_diameter": cytosol_diameter,
        }
        self._fill(payload, "nuclei", Path(nuclei_file), send_data)
        self._fill(payload, "cytosol", Path(cytosol_file), send_data)
        try:
            resp = self._post("/segment", payload, timeout=timeout)
        except HTTPError as e:
            body = e.read().decode("utf-8")
            ret = json.loads(body)
            raise RuntimeError(f"Failed to segment: {str(e)} - {ret['message']}")

        if save_to and resp.get("outputs"):
            import base64
            os.makedirs(save_to, exist_ok=True)
            for entry in resp["outputs"]:
                if "data" in entry and entry["name"] == "cell_mask":
                    dest = Path(save_to) / (output_prefix + "_" + entry["name"] + entry["suffix"])
                    with dest.open("wb") as fh:
                        fh.write(base64.b64decode(entry["data"]))
                    entry["path"] = dest
        elif save_to and resp.get("output_data"):
            import base64
            os.makedirs(save_to, exist_ok=True)
            dest = Path(save_to) / (output_prefix + "_" + resp.get("output_type") + "." + resp.get("output_image_type"))
            with dest.open("wb") as fh:
                fh.write(base64.b64decode(resp.get("output_data")))
        return resp

    @staticmethod
    def _fill(payload: Dict, channel: str, path: Path, send_data: bool):
        if send_data:
            import base64
            data = path.open("rb").read()
            payload[f"{channel}_data"] = base64.b64encode(data).decode("ascii")
            payload[f"{channel}_suffix"] = path.suffix
        elif path is not None:
            payload[f"{channel}_file"] = str(path)
        else:
            raise ValueError(f"{channel}: provide a *_file path or *_data bytes")


if __name__ == "__main__":
    # Tiny smoke test against the stub analyzer.
    import tempfile, os
    with CellposeService(stub=False) as svc:
        print("health:", svc._get("/health"))
        for i in sys.argv[1:]:
            print("segment:", i)
            nuc = f"{i}_blue.tif"
            cyto = f"{i}_red.tif"
            r = svc.segment(i,
                            nuclei_file=nuc, nuclei_diameter=30,
                            cytosol_file=cyto, cytosol_diameter=60,
                            send_data=True,
                            save_to="test")
            # print("path-based outputs:", [(o["name"], len(o["data"])) for o in r["outputs"]])
            print("files on disk:", os.listdir("test"))

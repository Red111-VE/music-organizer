"""Tests para :mod:`web.runner`: red de seguridad contra el deadlock por
stderr.

Regresión documentada: cuando el worker se lanzaba con
``stderr=asyncio.subprocess.PIPE`` y el runner solo leía stderr **después**
de ``process.wait()``, el worker se bloqueaba al llenar el buffer del pipe
(~64 KB) y ``wait()`` no retornaba nunca. Essentia/TensorFlow emiten miles
de warnings a stderr durante el análisis, así que la condición se disparaba
casi siempre en corridas reales.

El fix vive en :mod:`web.runner`: redirige ``stderr`` a un archivo temporal,
que no tiene el límite de buffer del pipe. Este test ejercita el flujo
completo de :func:`web.runner.start_pipeline` con un worker falso ruidoso —
si alguien vuelve a poner ``stderr=PIPE``, el test cuelga y dispara el
``TimeoutError`` de ``asyncio.wait_for``.

No usamos ``pytest-asyncio`` (no está en las deps). El test es síncrono y
arranca su propio event loop con ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from web import runner
from web.state import JobState


# Worker falso que imita el patrón ruidoso de Essentia + TensorFlow: unas
# pocas líneas a stdout (eventos JSON del pipeline) y MUCHO ruido a stderr.
#
# Cada línea de stderr ~280 bytes × 3000 líneas ≈ 820 KB — bastante por
# encima de los ~64 KB del buffer de un pipe en macOS/Linux. Si el runner
# usa stderr=PIPE sin reader concurrente, el worker bloquea su write a
# stderr antes de llegar al print de "done" y nunca cierra stdout. El
# ``async for`` del runner se queda esperando, ``wait_for`` corta a los
# 10 s con TimeoutError → test rojo.
_FAKE_WORKER = """\
import sys, json
print(json.dumps({"event": "start"}), flush=True)
junk = "essentia warning: lorem ipsum dolor sit amet consectetur adipiscing " * 4
for i in range(3000):
    print(f"line {i:04d}: {junk}", file=sys.stderr, flush=True)
print(json.dumps({"event": "phase", "phase": "tag", "total": 0}), flush=True)
print(json.dumps({"event": "phase", "phase": "enrich", "total": 0}), flush=True)
print(json.dumps({"event": "phase", "phase": "organize", "total": 0}), flush=True)
print(json.dumps({"event": "done", "tag_ok": 0, "tag_err": 0}), flush=True)
"""


def test_runner_survives_noisy_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """El runner completa aunque el worker escriba ~800 KB a stderr.

    Si vuelve la regresión (``stderr=PIPE`` en lugar de tempfile), el worker
    se bloquea escribiendo a stderr una vez lleno el buffer del pipe y
    ``process.wait()`` nunca retorna. El ``asyncio.wait_for(timeout=10.0)``
    detecta el cuelgue y falla con ``TimeoutError``.

    En el camino feliz (fix vigente) el test corre en <1 s.
    """
    # Worker falso a disco. ``sys.executable`` lo va a ejecutar como script
    # común — no necesita ser un módulo importable.
    fake_worker = tmp_path / "fake_worker.py"
    fake_worker.write_text(_FAKE_WORKER)

    # Aislamos del singleton real: el test usa su propio JobState para no
    # contaminar otros tests y para que las aserciones sean deterministas.
    fresh_state = JobState()
    monkeypatch.setattr(runner, "state", fresh_state)
    monkeypatch.setattr(runner, "_current_process", None)

    # Reemplazamos ``asyncio.create_subprocess_exec`` para que, en vez del
    # ``python -m web.worker ...`` real, lance nuestro script falso. Pero
    # mantenemos los ``kwargs`` (``stdout=PIPE``, ``stderr=<file>``) que
    # pasa el runner — eso es exactamente el patrón que queremos validar.
    original_exec = asyncio.create_subprocess_exec

    async def fake_exec(*_args: object, **kwargs: object) -> asyncio.subprocess.Process:
        return await original_exec(sys.executable, str(fake_worker), **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def _drive() -> None:
        await asyncio.wait_for(
            runner.start_pipeline(
                source=tmp_path,
                models_dir=tmp_path,
                dest=tmp_path,
                simulate=True,
            ),
            timeout=10.0,
        )

    asyncio.run(_drive())

    # Camino feliz: el fake emitió ``done`` → status==done. Si el runner se
    # hubiera colgado, ``wait_for`` ya habría tirado TimeoutError antes.
    assert fresh_state.status == "done", (
        f"esperaba status=done, obtuve {fresh_state.status!r} "
        f"(error={fresh_state.error!r})"
    )
    assert fresh_state.result == {
        "tag_ok": 0,
        "tag_err": 0,
        "report": None,
    }

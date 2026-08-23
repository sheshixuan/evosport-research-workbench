from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from evosport.data.manifest import DatasetManifest


def render_report(
    *,
    run_id: str,
    fingerprint: str,
    manifest: DatasetManifest,
    homerun_run_id: str,
    result: dict[str, Any],
    decision: dict[str, str],
    artifact_dir: Path,
) -> Path:
    output = artifact_dir / "report.html"
    output.write_bytes(
        render_report_bytes(
            run_id=run_id,
            fingerprint=fingerprint,
            manifest=manifest,
            homerun_run_id=homerun_run_id,
            result=result,
            decision=decision,
        )
    )
    return output


def render_report_bytes(
    *,
    run_id: str,
    fingerprint: str,
    manifest: DatasetManifest,
    homerun_run_id: str,
    result: dict[str, Any],
    decision: dict[str, str],
) -> bytes:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "j2"]),
    )
    execution = dict(result.get("execution") or {})
    html = env.get_template("experiment.html.j2").render(
        run_id=run_id,
        fingerprint=fingerprint,
        manifest=manifest,
        homerun_run_id=homerun_run_id,
        result=result,
        execution=execution,
        decision=decision,
    )
    return html.encode("utf-8")

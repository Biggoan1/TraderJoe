"""Tests for the environment launcher scripts and systemd examples.

Verifies:
- Each launcher loads ONLY its matching env file.
- No launcher ever sources ``.env`` or ``.env.production``.
- Launchers refuse to run when the target env file is missing.
- Launchers signal ``HERMES_CONTEXT``.
- Live-runner source files do not read research env vars.
- systemd examples wire ``EnvironmentFile=`` to the correct env file.
- The production systemd file is an example only and refuses to run.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

LAUNCHER_MATRIX: Dict[str, Tuple[str, str]] = {
    "run-paper": (".env.paper", "paper"),
    "run-crypto": (".env.crypto", "crypto"),
    "run-research": (".env.research", "research"),
}


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


def _script(name: str) -> str:
    return _read(f"scripts/{name}")


def _strip_python_comments_and_docstrings(source: str) -> str:
    """Strip Python `#` comments AND triple-quoted docstring/string
    blocks so source-safety scans skip descriptive text and only see
    executable code.

    This intentionally over-strips: any triple-quoted string
    (including a legitimate multi-line SQL literal, for example) is
    dropped.  Trader Joe modules do not currently use multi-line
    non-docstring string literals in a way this matters for.
    """
    # Drop triple-quoted blocks (docstrings and multi-line strings).
    without_triples = re.sub(
        r'(?s)"""(?:.*?)"""|\'\'\'(?:.*?)\'\'\'',
        "",
        source,
    )
    # Drop `#` line comments.
    lines: List[str] = []
    for line in without_triples.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        no_inline = re.sub(r"(?:^|\s)#.*$", "", line)
        lines.append(no_inline)
    return "\n".join(lines)


def _strip_shell_comments(source: str) -> str:
    """Drop `#`-comment lines and inline `# ...` tails from shell source."""
    lines: List[str] = []
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Preserve `#` inside quoted strings by only trimming when the
        # `#` follows whitespace or start-of-line.
        no_inline = re.sub(r"(?:^|\s)#.*$", "", line)
        lines.append(no_inline)
    return "\n".join(lines)


def _matches_bare_env_reference(source: str) -> List[str]:
    """Return lines that reference `.env` literally but not
    `.env.<context>`.

    We strip comments and known context env-file names first, then
    look for any remaining `.env` token.  This catches `. .env`,
    `source .env`, and `\".env\"` while ignoring safety comments that
    describe the isolation rule.
    """
    code = _strip_shell_comments(source)
    code = re.sub(
        r"\.env\.(paper|crypto|research|production|example)",
        "",
        code,
    )
    return [
        line
        for line in code.splitlines()
        if re.search(r"(?<![.\w])\.env(?![.\w])", line)
    ]


# ---------------------------------------------------------------------------
# Launcher source-content checks
# ---------------------------------------------------------------------------


class TestLauncherExistence:
    @pytest.mark.parametrize("script", list(LAUNCHER_MATRIX))
    def test_script_exists(self, script):
        path = REPO_ROOT / "scripts" / script
        assert path.is_file(), f"{path} is missing"

    @pytest.mark.parametrize("script", list(LAUNCHER_MATRIX))
    def test_script_is_executable(self, script):
        path = REPO_ROOT / "scripts" / script
        assert os.access(path, os.X_OK), f"{path} is not executable"

    def test_no_run_production_script(self):
        path = REPO_ROOT / "scripts" / "run-production"
        assert not path.exists(), (
            "scripts/run-production must not exist without explicit "
            "production approval"
        )


class TestLauncherSourcesOnlyMatchingEnvFile:
    @pytest.mark.parametrize(
        "script,env_file",
        [(name, matrix[0]) for name, matrix in LAUNCHER_MATRIX.items()],
    )
    def test_script_references_its_env_file(self, script, env_file):
        source = _script(script)
        assert env_file in source, (
            f"{script} does not reference {env_file}"
        )

    @pytest.mark.parametrize("script", list(LAUNCHER_MATRIX))
    def test_script_never_sources_env_production(self, script):
        source = _script(script)
        code = _strip_shell_comments(source)
        assert ".env.production" not in code, (
            f"{script} must not reference .env.production outside a comment"
        )

    @pytest.mark.parametrize("script", list(LAUNCHER_MATRIX))
    def test_script_never_sources_bare_dotenv(self, script):
        source = _script(script)
        bad_lines = _matches_bare_env_reference(source)
        assert not bad_lines, (
            f"{script} references a bare `.env` file: {bad_lines}"
        )

    @pytest.mark.parametrize(
        "script,other",
        [
            ("run-paper", ".env.crypto"),
            ("run-paper", ".env.research"),
            ("run-crypto", ".env.paper"),
            ("run-crypto", ".env.research"),
            ("run-research", ".env.paper"),
            ("run-research", ".env.crypto"),
        ],
    )
    def test_script_does_not_reference_other_context_env_files(
        self, script, other
    ):
        source = _script(script)
        assert other not in source, (
            f"{script} unexpectedly references {other}"
        )


class TestLauncherExportsContextMarker:
    @pytest.mark.parametrize(
        "script,context",
        [(name, matrix[1]) for name, matrix in LAUNCHER_MATRIX.items()],
    )
    def test_script_exports_hermes_context(self, script, context):
        source = _script(script)
        # Accept either a literal `HERMES_CONTEXT="<context>"` or the
        # canonical `CONTEXT="<context>"` + `export HERMES_CONTEXT="$CONTEXT"`
        # pattern the shipped launchers use.
        literal_ok = (
            f'HERMES_CONTEXT="{context}"' in source
            or f"HERMES_CONTEXT='{context}'" in source
        )
        indirect_ok = (
            f'CONTEXT="{context}"' in source
            and "export HERMES_CONTEXT=" in source
        )
        assert literal_ok or indirect_ok, (
            f"{script} does not export HERMES_CONTEXT={context}"
        )


class TestLauncherRefusesMissingEnvFile:
    @pytest.mark.parametrize(
        "script,env_file",
        [(name, matrix[0]) for name, matrix in LAUNCHER_MATRIX.items()],
    )
    def test_missing_env_file_exits_nonzero(
        self, script, env_file, tmp_path
    ):
        # Run in a tmp cwd so the target env file is missing.
        script_path = REPO_ROOT / "scripts" / script
        result = subprocess.run(
            ["bash", str(script_path)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (
            f"{script} should exit non-zero when {env_file} is missing"
        )
        assert env_file in result.stderr, (
            f"{script} did not name the missing env file in stderr"
        )


# ---------------------------------------------------------------------------
# Live-runner source safety
# ---------------------------------------------------------------------------


class TestLiveRunnerSafety:
    @pytest.mark.parametrize("file", ["trader.py", "crypto_trader.py"])
    def test_live_runner_code_never_references_research_env(self, file):
        source = _read(file)
        code = _strip_python_comments_and_docstrings(source)
        assert "RESEARCH_ALPACA" not in code, (
            f"{file} must not reference RESEARCH_ALPACA env vars in code"
        )
        assert "RESEARCH_AI_MODEL" not in code, (
            f"{file} must not reference RESEARCH_AI_MODEL in code"
        )
        assert ".env.research" not in code, (
            f"{file} must not reference .env.research in code"
        )

    @pytest.mark.parametrize("file", ["trader.py", "crypto_trader.py"])
    def test_live_runner_code_never_references_env_production(self, file):
        source = _read(file)
        code = _strip_python_comments_and_docstrings(source)
        assert ".env.production" not in code, (
            f"{file} must not load .env.production"
        )
        assert "PRODUCTION_AI_MODEL" not in code, (
            f"{file} must not read PRODUCTION_AI_MODEL"
        )


# ---------------------------------------------------------------------------
# systemd example units
# ---------------------------------------------------------------------------


SYSTEMD_MATRIX = {
    "traderjoe-paper.service": (".env.paper", "paper"),
    "traderjoe-crypto.service": (".env.crypto", "crypto"),
    "traderjoe-research.service": (".env.research", "research"),
}


class TestSystemdExamples:
    @pytest.mark.parametrize("unit", list(SYSTEMD_MATRIX))
    def test_unit_exists(self, unit):
        assert (REPO_ROOT / "docs" / "systemd" / unit).is_file()

    @pytest.mark.parametrize(
        "unit,env_file",
        [(name, matrix[0]) for name, matrix in SYSTEMD_MATRIX.items()],
    )
    def test_unit_environment_file_matches_context(self, unit, env_file):
        source = _read(f"docs/systemd/{unit}")
        assert f"EnvironmentFile=" in source
        assert env_file in source

    @pytest.mark.parametrize(
        "unit,context",
        [(name, matrix[1]) for name, matrix in SYSTEMD_MATRIX.items()],
    )
    def test_unit_sets_hermes_context(self, unit, context):
        source = _read(f"docs/systemd/{unit}")
        assert f"Environment=HERMES_CONTEXT={context}" in source


class TestProductionSystemdSafety:
    UNIT = "traderjoe-production.service.example"

    def test_production_unit_uses_example_suffix(self):
        path = REPO_ROOT / "docs" / "systemd" / self.UNIT
        assert path.is_file()
        # No .service twin
        service_only = REPO_ROOT / "docs" / "systemd" / "traderjoe-production.service"
        assert not service_only.exists(), (
            "traderjoe-production.service must not exist without approval"
        )

    def test_production_unit_starts_bin_false(self):
        source = _read(f"docs/systemd/{self.UNIT}")
        assert "ExecStart=/bin/false" in source, (
            "production unit must exec /bin/false to refuse to run"
        )

    def test_production_unit_requires_marker_file(self):
        source = _read(f"docs/systemd/{self.UNIT}")
        assert "ConditionPathExists=" in source, (
            "production unit must gate on a manually-created marker file"
        )

    def test_production_unit_omits_install_section(self):
        source = _read(f"docs/systemd/{self.UNIT}")
        # Any [Install] header must be either absent or commented out.
        for line in source.splitlines():
            if line.strip().startswith("[Install]"):
                pytest.fail(
                    "production unit must omit [Install] so `systemctl "
                    "enable` fails until an operator adds it explicitly"
                )

    def test_production_unit_names_real_money(self):
        source = _read(f"docs/systemd/{self.UNIT}")
        assert "REAL MONEY" in source


# ---------------------------------------------------------------------------
# .env.example content
# ---------------------------------------------------------------------------


class TestEnvExample:
    def test_env_example_lists_all_four_context_model_vars(self):
        source = _read(".env.example")
        assert "PAPER_AI_MODEL" in source
        assert "CRYPTO_AI_MODEL" in source
        assert "RESEARCH_AI_MODEL" in source
        assert "PRODUCTION_AI_MODEL" in source

    def test_env_example_holds_no_real_credentials(self):
        source = _read(".env.example")
        for pattern in (
            r"PKIZAKAVSYD6VNX2DNL3DZ5L5P",
            r"6hfir5D8HSoDdtz7bMsCVdDWE25hvFjKDp8jDFoh7St2",
        ):
            assert not re.search(pattern, source), (
                f".env.example must not contain {pattern}"
            )

    def test_env_example_marks_production_as_gated(self):
        source = _read(".env.example")
        assert "EMPTY UNTIL EXPLICIT PRODUCTION APPROVAL" in source

    def test_production_ai_model_line_is_commented(self):
        source = _read(".env.example")
        # The PRODUCTION_AI_MODEL entry must live inside the production
        # section and be commented out — it's a placeholder for the
        # approved-only future.
        for line in source.splitlines():
            if "PRODUCTION_AI_MODEL" in line:
                assert line.lstrip().startswith("#"), (
                    "PRODUCTION_AI_MODEL must be commented out in .env.example"
                )

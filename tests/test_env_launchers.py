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
from typing import Dict, List, Sequence, Tuple

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
        # Forbid patterns that would LOAD .env.production, not
        # patterns that REFUSE it (run-research has an explicit
        # `case` refusal for --env-file paths that resolve to
        # .env.production, and that must remain).
        forbidden_load_patterns = [
            r"source\s+[^\n]*\.env\.production",
            r"\.\s+[^\n]*\.env\.production",
            r"ENV_FILE\s*=\s*[\"']?\.env\.production",
            r"DEFAULT_ENV_FILENAME\s*=\s*[\"']?\.env\.production",
        ]
        for pattern in forbidden_load_patterns:
            assert not re.search(pattern, code), (
                f"{script} must not load .env.production "
                f"(matched pattern: {pattern!r})"
            )

    @pytest.mark.parametrize("script", list(LAUNCHER_MATRIX))
    def test_script_never_sources_bare_dotenv(self, script):
        source = _script(script)
        code = _strip_shell_comments(source)
        # Forbid patterns that would LOAD a bare `.env`, not patterns
        # that REFUSE it (run-research has a case refusal for
        # --env-file paths that resolve to a bare .env).
        forbidden_load_patterns = [
            r"source\s+[\"']?\.env[\"']?(?![.\w])",
            r"\.\s+[\"']?\.env[\"']?(?![.\w])",
            r"ENV_FILE\s*=\s*[\"']?\.env[\"']?(?![.\w])",
            r"DEFAULT_ENV_FILENAME\s*=\s*[\"']?\.env[\"']?(?![.\w])",
        ]
        for pattern in forbidden_load_patterns:
            assert not re.search(pattern, code), (
                f"{script} must not load a bare `.env` "
                f"(matched pattern: {pattern!r})"
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


def _stage_fake_repo(tmp_path: Path, script: str) -> Tuple[Path, Path]:
    """Copy a launcher into an isolated fake-repo tree.

    Returns ``(fake_repo_root, fake_script_path)``.  The fake repo has
    no ``.env.<context>`` files anywhere, so the launcher's
    ``REPO_ROOT`` search hits an empty directory and exits with the
    documented error.
    """
    fake_repo = tmp_path / "fake_repo"
    fake_scripts = fake_repo / "scripts"
    fake_scripts.mkdir(parents=True)
    src = REPO_ROOT / "scripts" / script
    dst = fake_scripts / script
    dst.write_bytes(src.read_bytes())
    dst.chmod(0o755)
    return fake_repo, dst


class TestLauncherRefusesMissingEnvFile:
    @pytest.mark.parametrize(
        "script,env_file",
        [(name, matrix[0]) for name, matrix in LAUNCHER_MATRIX.items()],
    )
    def test_missing_env_file_exits_nonzero(
        self, script, env_file, tmp_path
    ):
        # Isolate the launcher in a fake repo so the REPO_ROOT
        # fallback also fails to find the env file.  The launcher
        # searches cwd first, then REPO_ROOT — both empty here.
        fake_repo, fake_script = _stage_fake_repo(tmp_path, script)
        run_cwd = tmp_path / "cwd"
        run_cwd.mkdir()

        result = subprocess.run(
            ["bash", str(fake_script)],
            cwd=run_cwd,
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
# run-research env-file discovery
# ---------------------------------------------------------------------------


RESEARCH_FIXTURE_ENV_LINES = "\n".join(
    [
        "RESEARCH_ALPACA_API_KEY=test-api-key",
        "RESEARCH_ALPACA_SECRET_KEY=test-secret-key",
        "RESEARCH_ALPACA_ENDPOINT=https://research.example",
        "",
    ]
)


def _stage_research_fake_repo(tmp_path: Path) -> Tuple[Path, Path]:
    """Stage `run-research` inside a fake repo tree with NO env files."""
    return _stage_fake_repo(tmp_path, "run-research")


def _write_env_file(path: Path, extra: str = "") -> None:
    path.write_text(RESEARCH_FIXTURE_ENV_LINES + extra, encoding="utf-8")


def _run_research(
    fake_script: Path,
    cwd: Path,
    args: Sequence[str] = (),
) -> subprocess.CompletedProcess:
    """Run the launcher and forward an inline ``print('OK')`` to
    python by default so the exec at the end lands on something
    self-contained (no PYTHONPATH required).
    """
    forwarded = list(args) or ["-c", "print('research-launch OK')"]
    return subprocess.run(
        ["bash", str(fake_script), *forwarded],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class TestRunResearchEnvDiscovery:
    def test_launched_from_repo_root_finds_env_file_there(self, tmp_path):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        _write_env_file(fake_repo / ".env.research")

        result = _run_research(fake_script, cwd=fake_repo)
        assert result.returncode == 0, result.stderr
        assert "research-launch OK" in result.stdout

    def test_launched_from_scripts_dir_finds_env_at_repo_root(self, tmp_path):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        _write_env_file(fake_repo / ".env.research")
        scripts_dir = fake_repo / "scripts"

        result = _run_research(fake_script, cwd=scripts_dir)
        assert result.returncode == 0, result.stderr

    def test_launched_from_unrelated_dir_still_finds_env_at_repo_root(
        self, tmp_path
    ):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        _write_env_file(fake_repo / ".env.research")
        unrelated = tmp_path / "elsewhere"
        unrelated.mkdir()

        result = _run_research(fake_script, cwd=unrelated)
        assert result.returncode == 0, result.stderr

    def test_cwd_env_file_wins_over_repo_root(self, tmp_path):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        # Repo-root env file has a specific marker
        _write_env_file(
            fake_repo / ".env.research",
            extra="RESEARCH_ENV_SOURCE=repo-root\n",
        )
        # A cwd env file has a different marker
        cwd_dir = tmp_path / "workspace"
        cwd_dir.mkdir()
        _write_env_file(
            cwd_dir / ".env.research",
            extra="RESEARCH_ENV_SOURCE=cwd\n",
        )

        result = _run_research(
            fake_script,
            cwd=cwd_dir,
            args=["-c", "import os; print(os.environ['RESEARCH_ENV_SOURCE'])"],
        )
        assert result.returncode == 0, result.stderr
        assert "cwd" in result.stdout

    def test_custom_env_file_wins_over_cwd_and_repo_root(self, tmp_path):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        _write_env_file(
            fake_repo / ".env.research",
            extra="RESEARCH_ENV_SOURCE=repo-root\n",
        )
        cwd_dir = tmp_path / "workspace"
        cwd_dir.mkdir()
        _write_env_file(
            cwd_dir / ".env.research",
            extra="RESEARCH_ENV_SOURCE=cwd\n",
        )
        custom = tmp_path / "custom.research"
        _write_env_file(custom, extra="RESEARCH_ENV_SOURCE=custom\n")

        result = _run_research(
            fake_script,
            cwd=cwd_dir,
            args=[
                "--env-file",
                str(custom),
                "-c",
                "import os; print(os.environ['RESEARCH_ENV_SOURCE'])",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert "custom" in result.stdout

    def test_custom_env_file_equals_form(self, tmp_path):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        custom = tmp_path / "custom.research"
        _write_env_file(custom, extra="RESEARCH_ENV_SOURCE=eq-form\n")

        result = _run_research(
            fake_script,
            cwd=tmp_path,
            args=[
                f"--env-file={custom}",
                "-c",
                "import os; print(os.environ['RESEARCH_ENV_SOURCE'])",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert "eq-form" in result.stdout

    def test_custom_env_file_missing_reports_all_locations(self, tmp_path):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        # Nothing at custom, cwd, or repo root.
        result = _run_research(
            fake_script,
            cwd=tmp_path,
            args=["--env-file", str(tmp_path / "does_not_exist.research")],
        )
        assert result.returncode != 0
        assert "--env-file" in result.stderr
        assert "cwd" in result.stderr
        assert "repo root" in result.stderr
        assert ".env.research" in result.stderr

    def test_env_file_flag_without_value_errors(self, tmp_path):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        result = _run_research(
            fake_script, cwd=tmp_path, args=["--env-file"]
        )
        assert result.returncode != 0
        assert "requires a path" in result.stderr

    def test_refuses_env_file_named_dotenv(self, tmp_path):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        bare = tmp_path / ".env"
        _write_env_file(bare)
        result = _run_research(
            fake_script, cwd=tmp_path, args=["--env-file", str(bare)]
        )
        assert result.returncode != 0
        assert "refusing to load .env" in result.stderr

    def test_refuses_env_file_named_dotenv_production(self, tmp_path):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        prod = tmp_path / ".env.production"
        _write_env_file(prod)
        result = _run_research(
            fake_script, cwd=tmp_path, args=["--env-file", str(prod)]
        )
        assert result.returncode != 0
        assert "refusing to load .env.production" in result.stderr

    def test_never_falls_back_to_bare_dotenv(self, tmp_path):
        """A `.env` file at the repo root must not be loaded by
        run-research even when .env.research is absent.  The launcher
        should error out with the "not found" message.
        """
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        # Populate a stray .env (attacker or misconfiguration case)
        (fake_repo / ".env").write_text(
            "SHOULD_NOT_BE_LOADED=yes\n", encoding="utf-8"
        )
        result = _run_research(fake_script, cwd=fake_repo)
        assert result.returncode != 0
        assert ".env.research" in result.stderr

    def test_never_falls_back_to_dotenv_production(self, tmp_path):
        fake_repo, fake_script = _stage_research_fake_repo(tmp_path)
        (fake_repo / ".env.production").write_text(
            "SHOULD_NOT_BE_LOADED=yes\n", encoding="utf-8"
        )
        result = _run_research(fake_script, cwd=fake_repo)
        assert result.returncode != 0
        assert ".env.research" in result.stderr


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
        """Scan `.env.example` for credential-shaped strings.

        Uses shape-based patterns instead of specific rotated-key
        literals so no historical credential value ever needs to be
        tracked in the repo.  The patterns cover the two Alpaca
        credential shapes and the AWS access-key shape.
        """
        source = _read(".env.example")
        for label, pattern in (
            ("Alpaca PK-prefixed API key", r"PK[A-Z0-9]{18,32}"),
            (
                "quoted 40-56-char mixed-case alphanumeric (Alpaca secret shape)",
                r"['\"][A-Za-z0-9]{40,56}['\"]",
            ),
            ("AWS access key", r"AKIA[A-Z0-9]{16,}"),
        ):
            assert not re.search(pattern, source), (
                f".env.example must not contain {label} shape "
                f"(pattern={pattern!r})"
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

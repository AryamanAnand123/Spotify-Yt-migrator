"""Offline diagnostics and explicit account connection commands."""

import typer

from spottoyt import __version__
from spottoyt.config import data_directory

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False,
                  help="Archive Spotify and review YouTube Music migrations.")


@app.command()
def version() -> None:
    """Show the installed application version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Show offline setup status without opening files or contacting services."""
    try:
        directory = data_directory()
    except ValueError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Spottoyt {__version__}")
    typer.echo(f"Private data directory (not created): {directory}")
    typer.echo("Account integrations: available through explicit connect/check commands; not checked")
    typer.echo("Network requests: none; account changes: none")


def account_action(action):
    """Never dump third-party exception bodies containing credentials or account data."""
    from spottoyt.auth import SetupError

    try:
        return action()
    except SetupError as exc:
        typer.echo(str(exc), err=True)
    except (KeyboardInterrupt, EOFError):
        typer.echo("Cancelled.", err=True)
    except Exception as exc:
        from spottoyt.auth import safe_failure

        typer.echo(safe_failure(exc), err=True)
    raise typer.Exit(code=1)


@app.command()
def connect_spotify():
    """Authorize Spotify read access using PKCE; no client secret required."""
    from spottoyt.auth import Vault, connect_spotify as connect

    client_id = typer.prompt("Spotify client ID", hide_input=True).strip()
    if not client_id:
        raise typer.BadParameter("Client ID must not be empty.")
    account_action(lambda: connect(client_id, typer.prompt, typer.echo, Vault()))
    typer.echo("Spotify credentials saved in Windows Credential Manager. Run check-spotify.")


@app.command()
def check_spotify():
    """Read at most one page per library category; print no item names."""
    from spottoyt.auth import Vault, spotify_probes

    results = account_action(lambda: spotify_probes(Vault()))
    for label, result in results.items():
        typer.echo(f"{label}: {result}")
    typer.echo("History, playlist-content access, and podcast progress remain unverified.")


@app.command()
def connect_youtube():
    """Use Google device OAuth; account permission is broader than these read-only checks."""
    from spottoyt.auth import Vault, connect_youtube as connect

    typer.echo("Google OAuth grants broader YouTube permissions. Connection checks read data; transfers require explicit execution.")
    typer.confirm("Proceed with Google's consent flow?", abort=True)
    client_id = typer.prompt("Google OAuth client ID", hide_input=True).strip()
    secret = typer.prompt("Google OAuth client secret", hide_input=True).strip()
    if not client_id or not secret:
        raise typer.BadParameter("Both credential fields are required.")
    account_action(lambda: connect(client_id, secret, Vault()))
    typer.echo("YouTube credentials saved in Windows Credential Manager. Run check-youtube.")


@app.command()
def check_youtube():
    """Verify official YouTube OAuth with automatic token refresh; no library writes."""
    from spottoyt.youtube_official import YouTubeOfficial

    def check():
        client = YouTubeOfficial()
        client.get_account_info()
        entries = client.get_library_playlists(limit=1)
        return f"Official OAuth account verified; sample returned {len(entries)} playlist entries (not a total)."
    typer.echo(account_action(check))


@app.command()
def connect_youtube_browser():
    """Test and save a browser-session cookie locally, without writing a cookie file."""
    from spottoyt.auth import Vault, connect_youtube_browser as connect

    typer.echo("Browser-session credentials grant account access. Paste only into this hidden local prompt.")
    cookie = typer.prompt("Cookie request-header value", hide_input=True).strip()
    authuser = typer.prompt("X-Goog-AuthUser from the same request", default="0").strip()
    typer.echo(account_action(lambda: connect(cookie, authuser, Vault())))
    typer.echo("Saved in Windows Credential Manager. No library contents changed.")


@app.command()
def check_youtube_browser():
    """Read-only library check using the saved browser-session credential."""
    from spottoyt.auth import Vault, youtube_browser_probe

    typer.echo(account_action(lambda: youtube_browser_probe(Vault())))


@app.command()
def export_spotify():
    """Read Spotify library pages into a new private archive; no account writes."""
    from spottoyt.exporter import export_spotify as export

    path = account_action(lambda: export(progress=typer.echo))
    typer.echo(f"Saved archive and coverage reports: {path}")


@app.command()
def import_account(source: str):
    """Offline import of a Spotify download ZIP, JSON file, or folder into a new archive."""
    from spottoyt.importer import import_account as import_data

    path = account_action(lambda: import_data(source, progress=typer.echo))
    typer.echo(f"Saved account-download archive: {path}")


@app.command()
def match_music(archive: str | None = None, limit: int = 20, request_budget: int = 20, offline: bool = False):
    """Suggest music matches from a private API archive; no likes/saves/transfers."""
    from spottoyt.matching import match_music as match

    path = account_action(lambda: match(archive, limit, request_budget, offline, progress=typer.echo))
    typer.echo(f"Review report.json and review.csv in: {path}")


@app.command()
def match_podcasts(archive: str | None = None, limit: int = 20, request_budget: int = 20, offline: bool = False):
    """Suggest podcast show/episode matches; never subscribe, save, or change progress."""
    from spottoyt.podcasts import match_podcasts as match

    path = account_action(lambda: match(archive, limit, request_budget, offline, progress=typer.echo))
    typer.echo(f"Review report.json and review.csv in: {path}")


@app.command()
def demo(port: int = 8502, walkthrough: bool = False):
    """Run an isolated invented demo; --walkthrough proves the pipeline without a server."""
    from spottoyt.demo import run_demo

    account_action(lambda: run_demo(port, automated=walkthrough, echo=typer.echo))


@app.command()
def review(port: int = 8501):
    """Open the local review server. Use its printed URL; Ctrl+C stops it."""
    import subprocess
    from pathlib import Path
    from spottoyt.review import review_command

    command = account_action(lambda: review_command(port))
    typer.echo(f"Local review: http://127.0.0.1:{port} · transfers require explicit confirmation · Ctrl+C to stop")
    with subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2]) as process:
        try:
            code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            code = 0
    raise typer.Exit(code=code)


@app.command()
def prepare_transfer(also_save_to_library: bool = False, allow_partial_playlists: bool = True):
    """Freeze current approvals into an offline transfer plan. Performs no account writes."""
    from spottoyt.transfers import create_plan, load_plan, operation_counts

    folder = account_action(lambda: create_plan(also_save_to_library=also_save_to_library, allow_partial_playlists=allow_partial_playlists))
    plan = load_plan(folder.name)
    typer.echo(f"Private transfer preview: {folder / 'preview.md'}")
    typer.echo(f"Plan ID: {folder.name}")
    typer.echo(str(operation_counts(plan)))
    typer.echo(f"Held playlists: {len(plan['held_playlists'])}; unsearched songs: {plan['unsearched_music']}")


@app.command()
def preflight_transfer(plan_id: str):
    """Check the destination account, target availability and state without library writes."""
    from spottoyt.transfers import preflight

    checked = account_action(lambda: preflight(plan_id, progress=typer.echo))
    typer.echo("Preflight: " + checked["state"])
    if checked["state"] != "ready":
        typer.echo(checked.get("reason", "Destination checks failed."), err=True)
        raise typer.Exit(code=1)
    typer.echo("No account changes. Inspect the private plan and preflight before execution.")


@app.command()
def verify_transfer(plan_id: str, account_archive: list[str] | None = typer.Option(None)):
    """Read back a frozen plan and write private verification/gap reports; no account writes."""
    import json
    from spottoyt.reconciliation import verify_transfer as verify

    folder = account_action(lambda: verify(plan_id, account_archives=account_archive or (), progress=typer.echo))
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    typer.echo(f"Verification: {report['state']}; private reports: {folder}")
    if report["state"] == "blocked_or_partial":
        raise typer.Exit(code=1)


@app.command()
def execute_transfer(plan_id: str):
    """Execute a preflighted plan after an explicit local confirmation; resume the same plan safely."""
    from spottoyt.transfers import execute, load_plan, operation_counts

    plan = account_action(lambda: load_plan(plan_id))
    typer.echo(f"Plan {plan_id}: {operation_counts(plan)}")
    typer.echo("This changes your connected YouTube Music library. Spotify is read-only.")
    typer.confirm("Execute this concrete plan now?", abort=True)
    result = account_action(lambda: execute(plan_id, confirmed=True, progress=typer.echo))
    typer.echo(f"Execution: {result['state']}; verified operations: {result['completed']}/{result['total']}")


if __name__ == "__main__":
    app()

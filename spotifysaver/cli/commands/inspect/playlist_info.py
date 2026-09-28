"""Playlist Information Display Module.

This module provides functionality to display comprehensive playlist information
and metadata from Spotify playlists, including owner details, description,
and track count through the CLI interface.
"""

import click

from spotifysaver.models import Playlist


def show_playlist_info(playlist: Playlist, verbose: bool):
    """Display comprehensive playlist metadata and information.
    
    Shows formatted playlist information including name, creator/owner,
    description, track count, and optionally technical details like
    cover URL when verbose mode is enabled.
    
    Args:
        playlist (Playlist): The playlist object containing metadata to display
        verbose (bool): Whether to show detailed technical information including
                       cover URL and additional metadata
    """
    click.secho(f"\n🎧 Playlist: {playlist.name}", fg="green", bold=True)
    click.echo(f"🛠 Creator: {playlist.owner}")
    click.echo(f"📝 Description: {playlist.description or 'N/A'}")
    click.echo(f"🎵 Tracks: {len(playlist.tracks)}")
    
    click.echo("Tracklist:")
    for track in playlist.tracks:
        click.echo(f"  - {track.name} by {', '.join(track.artists)} ({track.duration // 60}:{track.duration % 60:02d})")

    if verbose:
        click.echo(f"\n🔍 Technical details:")
        click.echo(f"Cover URL: {playlist.cover_url or 'N/A'}")

import re
import unicodedata
from typing import Dict, List, Union
from spotifysaver.models.track import Track
from spotifysaver.spotlog import get_logger

class ScoreMatchCalculator:
    """
    Service to calculate match scores between YouTube Music results and Spotify tracks.
    """
    def __init__(self):
        self.logger = get_logger(f"{self.__class__.__name__}")

    def _similar(self, a: str, b: str) -> float:
        """Calculate similarity between strings (0-1) using SequenceMatcher.
        
        Args:
            a: First string to compare
            b: Second string to compare
            
        Returns:
            float: Similarity ratio between 0.0 and 1.0
        """
        from difflib import SequenceMatcher

        return SequenceMatcher(None, a, b).ratio()
    
    def _normalize(self, text: str) -> str:
        """Consistent text normalization for comparison.
        
        Removes common words and characters that might interfere with matching.
        
        Args:
            text: Text to normalize
            
        Returns:
            str: Normalized text string
        """
        text = "".join(
            character
            for character in unicodedata.normalize("NFKD", str(text))
            if not unicodedata.combining(character)
        )
        text = text.lower().replace("official", "").replace("video", "")
        text = re.sub(r"[^\w]|_", " ", text, flags=re.UNICODE)
        return " ".join(
            word for word in text.split() if word not in {"lyrics", "audio"}
        )
    
    def _score_duration(self, yt_duration: int, sp_duration: int) -> float:
        """
        Score based on duration difference.

        Args:
            yt_duration (int): YouTube Music duration in seconds
            sp_duration (int): Spotify track duration in seconds
            
        Returns:
            float: Duration score (0.0 to 0.3)
        """
        try:
            diff = abs(float(yt_duration) - float(sp_duration))
        except (TypeError, ValueError):
            return 0
        return 1 if diff <= 2 else max(0, 1 - (diff / 5)) * 0.3

    def _score_artist_overlap(self, yt_artists_raw: List[Dict], sp_artists: List[str]) -> float:
        """
        Score based on artist name overlap.
        
        Args:
            yt_artists_raw (List[Dict]): YouTube Music artist data
            sp_artists (List[str]): Spotify track artist names
            
        Returns:
            float: Artist overlap score (0.0 to 0.3)
        """
        yt_artists = {
            self._normalize(a.get("name", ""))
            for a in yt_artists_raw
            if isinstance(a, dict) and a.get("name")
        }
        yt_artists.discard("")
        sp_artists_set = {self._normalize(a) for a in sp_artists}
        sp_artists_set.discard("")
        overlap = len(yt_artists & sp_artists_set) / max(len(sp_artists_set), 1)
        main_artist = self._normalize(sp_artists[0]) if sp_artists else ""
        main_match = bool(main_artist) and main_artist in yt_artists
        return overlap * 0.3 + (0.1 if main_match else 0)

    def _score_title_similarity(self, yt_title: str, sp_title: str) -> float:
        """
        Score based on normalized title similarity and token overlap.
        
        Args:
            yt_title (str): YouTube Music title
            sp_title (str): Spotify track title
        Returns:
            float: Title similarity score (0.0 to 0.3)
        """
        norm_yt = self._normalize(yt_title)
        norm_sp = self._normalize(sp_title)
        if not norm_yt or not norm_sp:
            return 0
        similarity = self._similar(norm_yt, norm_sp)

        # Penalize if token overlap is weak
        yt_tokens = set(norm_yt.split())
        sp_tokens = set(norm_sp.split())
        token_overlap = len(yt_tokens & sp_tokens) / max(len(sp_tokens), 1)
        if token_overlap < 0.3:
            similarity *= 0.5

        return similarity * 0.3

    def _score_album_bonus(self, album_data: Union[str, Dict], sp_album: str) -> float:
        """
        Bonus if album name matches.
        
        Args:
            album_data (Union[str, Dict]): YouTube Music album data
            sp_album (str): Spotify album name
            
        Returns:
            float: Bonus score (0.1 or 0)
        """
        if not album_data or not sp_album:
            return 0
        album_name = (
            self._normalize(album_data["name"])
            if isinstance(album_data, dict)
            else self._normalize(str(album_data))
        )
        normalized_album = self._normalize(sp_album)
        return 0.1 if normalized_album and normalized_album in album_name else 0

    def _has_strong_metadata_match(
        self, yt_result: Dict, track: Track
    ) -> bool:
        """Return whether non-title metadata identifies the same recording."""
        try:
            duration_matches = abs(
                float(yt_result.get("duration_seconds")) - float(track.duration)
            ) <= 2
        except (TypeError, ValueError):
            duration_matches = False
        yt_artists = {
            self._normalize(artist.get("name", ""))
            for artist in yt_result.get("artists", [])
            if isinstance(artist, dict) and artist.get("name")
        }
        artist_matches = bool(track.artists) and self._normalize(track.artists[0]) in yt_artists
        album_data = yt_result.get("album")
        yt_album = (
            album_data.get("name", "")
            if isinstance(album_data, dict)
            else str(album_data or "")
        )
        album_matches = bool(track.album_name) and self._normalize(track.album_name) in self._normalize(yt_album)
        return duration_matches and artist_matches and album_matches

    def _calculate_match_score(
        self, yt_result: Dict, track: Track, strict: bool
    ) -> float:
        """
        Refined scoring system for matching YouTube Music results to Spotify tracks.
        
        Args:
            yt_result (Dict): YouTube Music result data
            track (Track): Spotify track data
            strict (bool): Whether to use strict scoring thresholds
            
        Returns:
            float: Match score between 0.0 and 1.0+
        """
        try:
            # 1. Duration score (30%)
            duration_score = self._score_duration(yt_result.get("duration_seconds", 0), track.duration)

            # 2. Artist score (40%)
            artist_score = self._score_artist_overlap(yt_result.get("artists", []), track.artists)

            # 3. Title score (30%)
            title_score = self._score_title_similarity(yt_result.get("title", ""), track.name)

            # 4. Album bonus (+0.1)
            album_bonus = self._score_album_bonus(yt_result.get("album"), track.album_name)

            # Final threshold check
            total_score = duration_score + artist_score + title_score + album_bonus

            # Early exit for very low title similarity (experimental)
            if title_score < 0.1 and not self._has_strong_metadata_match(yt_result, track):
                total_score = min(total_score, 0.5)

            # Logging breakdown
            self.logger.debug(f"Scoring result for '{yt_result.get('title', 'Unknown')}'")
            self.logger.debug(f"Duration score: {duration_score:.3f}")
            self.logger.debug(f"Artist score: {artist_score:.3f}")
            self.logger.debug(f"Title score: {title_score:.3f}")
            self.logger.debug(f"Album bonus: {album_bonus:.3f}")
            self.logger.debug(f"Total score: {total_score:.3f}")
            
            # Apply strict threshold
            # Fuzzy searches are already constrained by artist and track title;
            # allow soundtrack uploads with missing duration or album metadata.
            threshold = 0.7 if strict else 0.5
            return total_score if total_score >= threshold else 0

        except Exception as e:
            self.logger.error(f"Error calculating score: {str(e)}")
            self.logger.debug(f"Problematic result: {yt_result}")
            return 0

    def explain_score(self, yt_result: Dict, track: Track, strict: bool = False) -> Dict:
        """
        Explain the score breakdown for a given YouTube result and Spotify track.

        Args:
            yt_result (Dict): YouTube Music result data
            track (Track): Spotify track data
            strict (bool): Whether to use strict scoring thresholds
        
        Returns:
            dict: Breakdown of each component and total score
        """
        try:
            duration_score = self._score_duration(yt_result.get("duration_seconds", 0), track.duration)
            artist_score = self._score_artist_overlap(yt_result.get("artists", []), track.artists)
            title_score = self._score_title_similarity(yt_result.get("title", ""), track.name)
            album_bonus = self._score_album_bonus(yt_result.get("album"), track.album_name)

            total_score = duration_score + artist_score + title_score + album_bonus
            threshold = 0.7 if strict else 0.5
            passed = total_score >= threshold

            return {
                "yt_title": yt_result.get("title", ""),
                "yt_videoId": yt_result.get("videoId", ""),
                "duration_score": round(duration_score, 3),
                "artist_score": round(artist_score, 3),
                "title_score": round(title_score, 3),
                "album_bonus": round(album_bonus, 3),
                "total_score": round(total_score, 3),
                "threshold": threshold,
                "passed": passed,
            }

        except Exception as e:
            self.logger.error(f"Error explaining score: {str(e)}")
            return {"error": str(e)}

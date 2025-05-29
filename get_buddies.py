#!/usr/bin/env python3

import logging
import os
import sys
import time
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from signal import SIGINT, SIGTERM, signal
from sys import exit

import requests
import spotipy
from requests.exceptions import ConnectionError, HTTPError, TooManyRedirects
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_fixed,
)

BUDDY_PLAYLISTS = {}

SLEEP_MINUTES = int(os.getenv("SLEEP_MINUTES", "2"))
TRACK_REPLAY_PLAYLIST = os.getenv("TRACK_REPLAY_PLAYLIST", "False").lower() in (
    "true",
    "1",
)
TRACK_SELF = os.getenv("TRACK_SELF", "False").lower() in ("true", "1")


def setup_logger():
    log_formatter = logging.Formatter(
        "%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s"
    )
    module_logger = logging.getLogger(__name__)
    module_logger.setLevel(logging.DEBUG)

    file_handler_all = logging.FileHandler("debug.log")
    file_handler_all.setFormatter(log_formatter)
    file_handler_all.setLevel(logging.DEBUG)
    module_logger.addHandler(file_handler_all)

    file_handler_info = logging.FileHandler("info.log")
    file_handler_info.setFormatter(log_formatter)
    file_handler_info.setLevel(logging.INFO)
    module_logger.addHandler(file_handler_info)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(os.getenv("LOGLEVEL", "INFO").upper())
    module_logger.addHandler(console_handler)

    return module_logger


LOGGER = setup_logger()


def handler(_signal_received, _frame):
    print("\nGot SIGINT. Exiting! ✨")
    exit(0)


def current_milli_time():
    return int(round(time.time() * 1000))


def _sleep():
    LOGGER.debug(f"Sleeping for {SLEEP_MINUTES} min...")
    time.sleep(SLEEP_MINUTES * 60)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=4, max=10),
    retry=retry_if_exception_type(JSONDecodeError),
    reraise=True,
)
def get_totp():
    try:
        r = requests.get("https://totp-gateway.glitch.me/create", timeout=5)
        r.raise_for_status()
        data = r.json()
        totp = data.get("totp")
        timestamp = data.get("timestamp")
        if not totp or not timestamp:
            LOGGER.error("TOTP or timestamp missing in response.")
            return None, None
        return totp, timestamp
    except (requests.RequestException, ValueError) as err:
        LOGGER.error(f"Failed to get TOTP: {err}")
        return None, None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=4, max=10),
    retry=retry_if_exception_type(JSONDecodeError),
    reraise=True,
)
def get_web_token(cookie, totp, timestamp):
    try:
        r = requests.get(
            f"https://open.spotify.com/get_access_token?reason=init&productType=web_player&totpVer=5&totp={totp}&cTime={timestamp}",
            cookies={"sp_dc": f"{cookie}"},
            timeout=5,
        )

    except (HTTPError, TooManyRedirects) as err:
        if isinstance(err, TooManyRedirects):
            print(f"No valid Cookie supplied:\n=> '{cookie}'")
            exit(1)
        LOGGER.error(err)
        return "", 0
    if r.status_code != 200:
        LOGGER.error("Invalid cookie or TOTP. Please check your cookie and try again.")
        LOGGER.error(f"Response: {r.text}")
        exit(1)
    return r.json()["accessToken"], int(r.json()["accessTokenExpirationTimestampMs"])


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=4, max=10),
    retry=retry_if_exception_type(ConnectionError),
    reraise=True,
)
def get_buddylist(token):
    try:
        r = requests.get(
            "https://guc-spclient.spotify.com/presence-view/v1/buddylist",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except HTTPError as err:
        LOGGER.error(err)
        return {}
    return r.json()


def parse_buddylist(buddylist):
    current_songs = {}
    try:
        for friend in buddylist["friends"]:
            current_songs[friend["user"]["name"]] = friend["track"]["uri"]
    except KeyError as err:
        LOGGER.error(err)
        LOGGER.error(buddylist)
        return ""

    return current_songs


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(7),
    retry=retry_if_exception_type(spotipy.exceptions.SpotifyException),
    reraise=True,
)
def init(cookie):
    token, refreh_time = get_web_token(cookie, *get_totp())
    sp = spotipy.Spotify(auth=token, requests_timeout=5, retries=4, backoff_factor=0.2)

    try:
        sp.current_user()
    except spotipy.exceptions.SpotifyException as err:
        if err.http_status == 401:
            LOGGER.error(
                "Invalid token. Please check your cookie and try again. (Retrying...)"
            )
            raise err
        else:
            LOGGER.error(f"Unexpected error: {err}")
            exit(1)

    return token, refreh_time, sp


def create_new_playlist(sp, playlist_name):
    if playlist_id := playlist_exists(sp, playlist_name):
        BUDDY_PLAYLISTS[playlist_name] = playlist_id
        return playlist_id

    try:
        user = sp.me()["id"]
        sp.user_playlist_create(user, playlist_name, public=False)
        current_playlist = sp.user_playlists(user, limit=1)
        BUDDY_PLAYLISTS[current_playlist["items"][0]["name"]] = current_playlist[
            "items"
        ][0]["id"]
    except ConnectionError as err:
        LOGGER.error(err)
        return ""

    return current_playlist["items"][0]["id"]


def playlist_exists(sp, playlist_name):
    try:
        playlists = sp.current_user_playlists()
    except ConnectionError as err:
        LOGGER.error(err)
        return None

    while playlists:
        for playlist in playlists["items"]:
            if playlist_name == playlist["name"]:
                return playlist["id"]
        playlists = sp.next(playlists)

    return None


def has_to_be_added(sp, playlist_id, song):
    try:
        songs = sp.playlist_items(playlist_id, fields="items,next")
    except ConnectionError as err:
        LOGGER.error(err)
        return False

    while songs:
        for s in songs.get("items", []):
            if song == s["track"]["uri"]:
                return False
        songs = sp.next(songs)

    return True


def has_to_be_added_replay(sp, playlist_id, song):
    last_song_index = sp.playlist_items(playlist_id, fields="total")["total"] - 1

    if last_song_index < 0:
        return True

    last_song_uri = sp.playlist_items(
        playlist_id, offset=last_song_index, fields="items.track.uri"
    )["items"][0]["track"]["uri"]

    return song != last_song_uri


def add_to_playlist(sp, current_songs):
    for name, song in current_songs.items():
        if is_local_song(song):
            continue
        if BUDDY_PLAYLISTS.get(f"Feed_{name}") is None:
            playlist_id = create_new_playlist(sp, f"Feed_{name}")
        else:
            playlist_id = BUDDY_PLAYLISTS[f"Feed_{name}"]

        if TRACK_REPLAY_PLAYLIST:
            add_to_replay_playlist(sp, name, song)

        if has_to_be_added(sp, playlist_id, song):
            try:
                add_song_to_playlist(sp, playlist_id, f"Feed_{name}", song)
            except ConnectionError as err:
                LOGGER.error(err)
                return

            LOGGER.info(f"Add '{song}' to Feed_{name}")
        else:
            LOGGER.info(f"No change in Feed for {name}")


def add_to_replay_playlist(sp, name, song):
    if is_local_song(song):
        return

    if BUDDY_PLAYLISTS.get(f"Replay_{name}") is None:
        playlist_id = create_new_playlist(sp, f"Replay_{name}")
    else:
        playlist_id = BUDDY_PLAYLISTS[f"Replay_{name}"]

    if has_to_be_added_replay(sp, playlist_id, song):
        try:
            add_song_to_playlist(sp, playlist_id, f"Replay_{name}", song)
        except ConnectionError as err:
            LOGGER.error(err)
            return

        LOGGER.info(f"Add '{song}' to Replay_{name}")


def rename_playlist(sp, playlist_id, playlist_name):
    new_name = f"{playlist_name}_{datetime.now().strftime('%Y-%m-%d')}"
    LOGGER.info(f"Rename Playlist: {playlist_name} -> {new_name}")
    sp.playlist_change_details(playlist_id, name=new_name)


def add_song_to_playlist(sp, playlist_id, playlist_name, song):
    try:
        sp.playlist_add_items(playlist_id, [song])
    except spotipy.exceptions.SpotifyException as err:
        if err.http_status == 400 and "Playlist size limit reached" in err.msg:
            rename_playlist(sp, playlist_id, playlist_name)
            new_playlist_id = create_new_playlist(sp, playlist_name)
            BUDDY_PLAYLISTS[playlist_name] = new_playlist_id
            sp.playlist_add_items(new_playlist_id, [song])


def is_local_song(song: str):
    if "spotify:local:" in song:
        LOGGER.info(f"{song} is local")
        return True

    return False


def refresh_token(cookie):
    token, refresh_time = get_web_token(cookie, *get_totp())
    return token, refresh_time


def main(cookie):
    token, refresh_time, sp = init(cookie)
    print("Running. Press CTRL-C to exit.\n")

    last_current_songs = None
    last_own_current_song = None
    own_name = sp.current_user()["display_name"]
    LOGGER.info(f"Entering main loop. Sleep time is set to {SLEEP_MINUTES} min.")
    while True:
        try:
            if refresh_time - current_milli_time() < 2000:
                LOGGER.info("Refresh token")
                try:
                    token, refresh_time = refresh_token(cookie)
                except JSONDecodeError as err:
                    LOGGER.error(f"Error after retry: {str(err)}")
                    _sleep()
                    continue

                sp.set_auth(token)

            try:
                buddylist = get_buddylist(token)
            except ConnectionError as err:
                LOGGER.error(f"Error after retry: {str(err)}")
                _sleep()
                continue

            if TRACK_SELF:
                result = sp.current_user_playing_track()
                if result is not None:
                    own_current_song = result["item"]["uri"]
                    if last_own_current_song != own_current_song:
                        add_to_replay_playlist(sp, own_name, own_current_song)
                        last_own_current_song = own_current_song
                    else:
                        LOGGER.debug("No own changes")

            current_songs = parse_buddylist(buddylist)
            LOGGER.debug(current_songs)
            if last_current_songs != current_songs:
                add_to_playlist(sp, current_songs)
                last_current_songs = current_songs
            else:
                LOGGER.debug("No changes")
            _sleep()
        except Exception as err:
            LOGGER.exception("Error in main loop:")
            _sleep()
            continue


if __name__ == "__main__":
    signal(SIGINT, handler)
    signal(SIGTERM, handler)
    try:
        cookie = Path("cookie.txt").read_text().replace("\n", "")
    except FileNotFoundError as err:
        cookie = os.getenv("SPOTIFY_COOKIE")
    main(cookie)

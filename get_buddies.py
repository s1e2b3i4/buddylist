#!/usr/bin/env python3

import sys
import requests
import spotipy
import time
import logging
from datetime import datetime
from signal import signal, SIGINT
from sys import exit
from pathlib import Path
from requests.exceptions import ConnectionError, HTTPError, TooManyRedirects
import os
from json import JSONDecodeError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

BUDDY_PLAYLISTS = {}

SLEEP_MINUTES = int(os.getenv("SLEEP_MINUTES", "2"))
TRACK_REPLAY_PLAYLIST = os.getenv("TRACK_REPLAY_PLAYLIST", "False").lower() in ("true", "1")
TRACK_SELF = os.getenv("TRACK_SELF", "False").lower() in ("true", "1")


def handler(_signal_received, _frame):
    print("\nGot SIGINT. Exiting! ✨")
    exit(0)


def current_milli_time():
    return int(round(time.time() * 1000))


def _sleep():
    logging.debug(f"Sleeping for {SLEEP_MINUTES} min...")
    time.sleep(SLEEP_MINUTES * 60)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=4, max=10),
    retry=retry_if_exception_type(JSONDecodeError),
    reraise=True,
)
def get_web_token(cookie):
    try:
        r = requests.get(
            "https://open.spotify.com/get_access_token?reason=transport&productType=web_player",
            cookies={"sp_dc": f"{cookie}"},
            timeout=5,
        )

    except (HTTPError, TooManyRedirects) as err:
        if isinstance(err, TooManyRedirects):
            print(f"No valid Cookie supplied:\n=> '{cookie}'")
            exit(1)
        logging.error(err)
        return "", 0
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
        logging.error(err)
        return {}
    return r.json()


def parse_buddylist(buddylist):
    current_songs = {}
    try:
        for friend in buddylist["friends"]:
            current_songs[friend["user"]["name"]] = friend["track"]["uri"]
    except KeyError as err:
        logging.error(err)
        logging.error(buddylist)
        return ""

    return current_songs


def init(cookie):
    token, refreh_time = get_web_token(cookie)
    sp = spotipy.Spotify(auth=token, requests_timeout=5, retries=4, backoff_factor=0.2)

    return token, refreh_time, sp


def create_new_playlist(sp, playlist_name):
    if playlist_id := playlist_exists(sp, playlist_name):
        BUDDY_PLAYLISTS[playlist_name] = playlist_id
        return playlist_id

    try:
        user = sp.me()["id"]
        sp.user_playlist_create(user, playlist_name, public=False)
        current_playlist = sp.user_playlists(user, limit=1)
        BUDDY_PLAYLISTS[current_playlist["items"][0]["name"]] = current_playlist["items"][0]["id"]
    except ConnectionError as err:
        logging.error(err)
        return ""

    return current_playlist["items"][0]["id"]


def playlist_exists(sp, playlist_name):
    try:
        playlists = sp.current_user_playlists()
    except ConnectionError as err:
        logging.error(err)
        return None

    while playlists:
        for playlist in playlists["items"]:
            if playlist_name == playlist["name"]:
                return playlist["id"]
        playlists = sp.next(playlists)

    return None


def has_to_be_added(sp, playlist_id, song):
    try:
        songs = sp.playlist(playlist_id, fields="tracks,next")["tracks"]
    except ConnectionError as err:
        logging.error(err)
        return False

    while songs:
        for s in songs["items"]:
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
                logging.error(err)
                return

            logging.info(f"Add '{song}' to Feed_{name}")
        else:
            logging.info(f"No change in Feed for {name}")


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
            logging.error(err)
            return

        logging.info(f"Add '{song}' to Replay_{name}")


def rename_playlist(sp, playlist_id, playlist_name):
    new_name = f"{playlist_name}_{datetime.now().strftime('%Y-%m-%d')}"
    logging.info(f"Rename Playlist: {playlist_name} -> {new_name}")
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
        logging.info(f"{song} is local")
        return True

    return False


def refresh_token(cookie):
    token, refresh_time = get_web_token(cookie)
    return token, refresh_time


def setup_logger():
    log_formatter = logging.Formatter(
        "%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s"
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.NOTSET)

    file_handler_all = logging.FileHandler("debug.log")
    file_handler_all.setFormatter(log_formatter)
    file_handler_all.setLevel(logging.NOTSET)
    root_logger.addHandler(file_handler_all)

    file_handler_info = logging.FileHandler("info.log")
    file_handler_info.setFormatter(log_formatter)
    file_handler_info.setLevel(logging.INFO)
    root_logger.addHandler(file_handler_info)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(os.getenv("LOGLEVEL", "INFO").upper())
    root_logger.addHandler(console_handler)


def main(cookie):
    setup_logger()

    token, refresh_time, sp = init(cookie)
    print("Running. Press CTRL-C to exit.\n")

    last_current_songs = None
    last_own_current_song = None
    own_name = sp.current_user()["display_name"]
    logging.info(f"Entering main loop. Sleep time is set to {SLEEP_MINUTES} min.")
    while True:
        try:
            if refresh_time - current_milli_time() < 2000:
                logging.info("Refresh token")
                try:
                    token, refresh_time = refresh_token(cookie)
                except JSONDecodeError as err:
                    logging.error(f"Error after retry: {str(err)}")
                    _sleep()
                    continue

                sp.set_auth(token)

            try:
                buddylist = get_buddylist(token)
            except ConnectionError as err:
                logging.error(f"Error after retry: {str(err)}")
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
                        logging.debug("No own changes")

            current_songs = parse_buddylist(buddylist)
            logging.debug(current_songs)
            if last_current_songs != current_songs:
                add_to_playlist(sp, current_songs)
                last_current_songs = current_songs
            else:
                logging.debug("No changes")
            _sleep()
        except Exception as err:
            logging.error(f"Error in main loop: {str(err)}")
            _sleep()
            continue


if __name__ == "__main__":
    signal(SIGINT, handler)
    try:
        cookie = Path("cookie.txt").read_text().replace("\n", "")
    except FileNotFoundError as err:
        cookie = os.getenv("SPOTIFY_COOKIE")
    main(cookie)

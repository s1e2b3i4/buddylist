#!/usr/bin/env python3

import sys
import requests
import spotipy
import time
import logging
from signal import signal, SIGINT
from sys import exit
from requests.exceptions import ConnectionError, HTTPError, TooManyRedirects
import os
from json import JSONDecodeError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

LOGLEVEL = os.getenv("LOGLEVEL", "INFO").upper()
SLEEP_MINUTES = 2
BUDDY_PLAYLISTS = dict()
logging.basicConfig(level=LOGLEVEL, format="%(asctime)s - %(levelname)s: %(name)s - %(message)s")

REPLAY_PLAYLIST_ENABLED = True
REPLAY_SELF = True


def handler(_signal_received, _frame):
    print("\nGot SIGINT. Exiting! ✨")
    exit(0)


def current_milli_time():
    return int(round(time.time() * 1000))


def _sleep():
    logging.debug(f"Sleeping for {SLEEP_MINUTES} min...")
    time.sleep(SLEEP_MINUTES*60)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1.5, min=4, max=10), retry=retry_if_exception_type(JSONDecodeError), reraise=True)
def get_web_token(cookie):
    try:
        r = requests.get(
            "https://open.spotify.com/get_access_token?reason=transport&productType=web_player",
            cookies={f"sp_dc": f"{cookie}"},
            timeout=5,
        )
    except (HTTPError, TooManyRedirects) as err:
        if isinstance(err, TooManyRedirects):
            print(f"No valid Cookie supplied:\n=> '{cookie}'")
            exit(1)
        logging.error(err)
        return "", 0
    return r.json()["accessToken"], int(r.json()["accessTokenExpirationTimestampMs"])

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1.5, min=4, max=10), retry=retry_if_exception_type(ConnectionError), reraise=True)
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
    current_songs = dict()
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
        BUDDY_PLAYLISTS[playlist_name] =  playlist_id
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
    lastSongIndex = sp.playlist_items(playlist_id, fields="total")["total"] - 1

    if(lastSongIndex < 0): return True

    lastSongUri = sp.playlist_items(playlist_id,offset=lastSongIndex,fields='items.track.uri')["items"][0]["track"]["uri"]

    return song != lastSongUri

def add_to_playlist(sp, current_songs):
    for name, song in current_songs.items():
        playlist_id = ""
        if BUDDY_PLAYLISTS.get(f"Feed_{name}") is None:
            playlist_id = create_new_playlist(sp, f"Feed_{name}")
        else:
            playlist_id = BUDDY_PLAYLISTS[f"Feed_{name}"]

        if(REPLAY_PLAYLIST_ENABLED):
            add_to_replay_playlist(sp, name, song)

        if has_to_be_added(sp, playlist_id, song):
            try:
                sp.playlist_add_items(playlist_id, [song])
            except ConnectionError as err:
                logging.error(err)
                return

            logging.info(f"Add '{song}' to Feed_{name}")
        else:
            logging.info(f"No change in Feed for {name}")

def add_to_replay_playlist(sp, name, song):
        playlist_id = ""
        if BUDDY_PLAYLISTS.get(f"Replay_{name}") is None:
            playlist_id = create_new_playlist(sp, f"Replay_{name}")
        else:
            playlist_id = BUDDY_PLAYLISTS[f"Replay_{name}"]

        if(has_to_be_added_replay(sp, playlist_id, song)):
            try:
                sp.playlist_add_items(playlist_id, [song])
            except ConnectionError as err:
                logging.error(err)
                return

            logging.info(f"Add '{song}' to Replay_{name}")    


def refresh_token(cookie):
    token, refresh_time = get_web_token(cookie)
    return token, refresh_time

def setup_logger():
    logFormatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
    rootLogger = logging.getLogger()
    rootLogger.setLevel(logging.NOTSET)

    fileHandlerAll = logging.FileHandler("debug.log")
    fileHandlerAll.setFormatter(logFormatter)
    fileHandlerAll.setLevel(logging.NOTSET)
    rootLogger.addHandler(fileHandlerAll)

    fileHandlerInfo = logging.FileHandler("info.log")
    fileHandlerInfo.setFormatter(logFormatter)
    fileHandlerInfo.setLevel(logging.INFO)
    rootLogger.addHandler(fileHandlerInfo)

    consoleHandler = logging.StreamHandler(sys.stdout)
    consoleHandler.setFormatter(logFormatter)
    consoleHandler.setLevel(logging.INFO)
    rootLogger.addHandler(consoleHandler)

def main(cookie):
    setup_logger()

    token, refresh_time, sp = init(cookie)
    print("Running. Press CTRL-C to exit.\n")

    last_current_songs = None
    last_own_current_song = None
    own_name = sp.current_user()["display_name"]
    logging.info(f"Entering main loop. Sleep time is set to {SLEEP_MINUTES} min.")
    while True:
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

        if REPLAY_SELF :
            own_current_song = sp.current_user_playing_track()["item"]["uri"]
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


if __name__ == "__main__":
    signal(SIGINT, handler)
    with open('cookie.txt', 'r') as file:
        cookie = file.read().replace('\n', '')
        main(cookie)   

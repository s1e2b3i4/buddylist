# Buddylist

Fetch your friends' Spotify feed and add the songs they listen to a dedicated playlist.

## Overview

This project is motivated by the [spotify-buddylist](https://github.com/valeriangalliat/spotify-buddylist) project, which allows getting the listening information of your friends programmatically.
From this idea on, I wanted to save what my friends are listening to in a playlist for me to check out later and discover new music.
Therefore, I use the rest of the [Spotify API ](https://developer.spotify.com/documentation/web-api/) to store those songs in a playlist.

The script will automatically check regularly if your friends are listening to a different song, and if it is not already in the playlist, it will add it.
Each friend gets a playlist called `Feed_<username>`.
You could then create a Folder in Spotify with all of these playlists to keep things organized.

## Usage

The first thing needed is the `sp_dc` cookie, which you can get by using the Spotify web player.
A detailed description of getting the cookie can be found [here](https://github.com/valeriangalliat/spotify-buddylist#sp_dc-cookie).

After that, the script requires the `SPOTIFY_COOKIE` environment variable to be set to the value of the `sp_dc` cookie.

## Example usage with a native install

```sh
pip install -r requirements.txt
SPOTIFY_COOKIE=YOUR-SPOTIFY-COOKIE python3 get_buddies.py
```

## Example usage with Docker install

```sh
docker pull s1e2b3i4/buddylist
docker run --env SPOTIFY_COOKIE=YOUR-SPOTIFY-COOKIE s1e2b3i4/buddylist
```

## Example usage with local Docker build

```sh
docker build -t buddylist .
docker run --env SPOTIFY_COOKIE=YOUR-SPOTIFY-COOKIE buddylist
```

## Configuration

The `LOGLEVEL` environment variable can be changed to `DEBUG` to increase the log output.

## Notice

The `sp_dc` cookie will be valid for _1 year_.
After that, you have to get a new cookie from the Spotify web player.

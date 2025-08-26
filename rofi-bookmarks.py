#!/usr/bin/env python3

import sqlite3
import subprocess
from argparse import ArgumentParser
from configparser import ConfigParser
from os import environ
from pathlib import Path
from hashlib import sha256
from contextlib import closing, contextmanager, suppress
from tempfile import NamedTemporaryFile
from shutil import copyfile
import glob

cache_dir = Path(environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'rofi-bookmarks'
firefox_dir = Path.home() / '.var/app/org.mozilla.firefox/.mozilla/firefox'

# b/c sqlite databases are locked by firefox we need copy them into a temporary location and connect to them there
@contextmanager
def temp_sqlite(path):
    with NamedTemporaryFile() as temp_loc:
        copyfile(path, temp_loc.name)
        with closing(sqlite3.connect(temp_loc.name)) as conn:
            yield conn

# Find profile directories by scanning the firefox directory structure
def find_profile_directories():
    """Find all profile directories in the firefox folder"""
    profile_dirs = []
    
    # Look for directories that match typical Firefox profile patterns
    # Firefox profiles usually end with .default or .default-release or have random strings
    for path in firefox_dir.glob('*'):
        if path.is_dir() and not path.name.startswith('.'):
            # Check if this looks like a profile directory by looking for key files
            if (path / 'places.sqlite').exists() or (path / 'prefs.js').exists():
                profile_dirs.append(path)
    
    return profile_dirs

# Get the default profile (first one found, or most recently modified)
def default_profile_path():
    """Get the default profile path by scanning directory structure"""
    profile_dirs = find_profile_directories()
    
    if not profile_dirs:
        raise Exception("No Firefox profiles found in directory structure")
    
    # Try to find default profile by looking at directory names first
    for profile_dir in profile_dirs:
        if 'default' in profile_dir.name.lower():
            return profile_dir
    
    # If no default found, return the most recently modified profile
    return max(profile_dirs, key=lambda p: p.stat().st_mtime)

# get Path to profile directory from profile name
def path_from_name(name):
    """Find profile path by name, scanning directory structure"""
    profile_dirs = find_profile_directories()
    
    # First try exact name match
    for profile_dir in profile_dirs:
        if profile_dir.name == name:
            return profile_dir
    
    # Try partial name match (useful for profiles like "default-release")
    for profile_dir in profile_dirs:
        if name.lower() in profile_dir.name.lower():
            return profile_dir
    
    # If still not found, try reading profiles.ini as fallback
    try:
        profiles = ConfigParser()
        profiles.read(firefox_dir / 'profiles.ini')
        for section in profiles.values():
            with suppress(KeyError):
                if section['Name'] == name:
                    return firefox_dir / section['Path']
    except:
        pass
    
    raise Exception(f"No profile with name '{name}' found")

# add icon file to cache (in ~/.cache/rofi-bookmarks)
def cache_icon(icon):
    loc = cache_dir / sha256(icon).hexdigest()
    if not cache_dir.exists():
        cache_dir.mkdir()
    if not loc.exists():
        loc.write_bytes(icon)
    return loc

# main function, finds all bookmaks inside of search_path and their corresponding icons and prints them in a rofi readable form
def write_rofi_input(profile_loc, search_path=[], sep=' / '):
    places_db = profile_loc / 'places.sqlite'
    favicons_db = profile_loc / 'favicons.sqlite'
    
    # Check if required database files exist
    if not places_db.exists():
        raise Exception(f"places.sqlite not found in profile: {profile_loc}")
    
    with temp_sqlite(places_db) as places:
        conn_res = places.execute("""SELECT moz_bookmarks.id, moz_bookmarks.parent, moz_bookmarks.type, moz_bookmarks.title, moz_places.url
                                     FROM moz_bookmarks LEFT JOIN moz_places ON moz_bookmarks.fk=moz_places.id
                                  """).fetchall()

    by_id = {i: (title, parent) for i, parent, _, title, _ in conn_res}
    def parent_generator(i):  # gives generator, where next is title of parent
        while i > 1:
            title, i = by_id[i]
            yield title

    # Only try to access favicons if the database exists
    favicon_available = favicons_db.exists()
    
    if favicon_available:
        favicon_context = temp_sqlite(favicons_db)
    else:
        favicon_context = suppress()  # No-op context manager
        
    with favicon_context as favicons:
        for index, parent, type, title, url in conn_res:
            if type == 1:  # type one means bookmark

                path_arr = reversed(list(parent_generator(index)))        # consumes beginning of path_arr and check if matches search_path (which implies path_arr is in a subfolder of seach_path)

                if all(name == next(path_arr) for name in search_path):   # this is safe, because next would only error if path_arr was a 'subpath' of search_path,
                    path = sep.join(list(path_arr))                        # but bookmarks are leaves ie don't have children
                    display_name = title if title else "Untitled Bookmark"
                    
                    icon = None
                    if favicon_available and favicons:
                        try:
                            icon = favicons.execute(f"""SELECT max(ic.data) FROM moz_pages_w_icons pg, moz_icons_to_pages rel, moz_icons ic
                                                        WHERE pg.id = rel.page_id AND ic.id=rel.icon_id AND pg.page_url=?
                                                     """ , (url,)).fetchone()[0]
                        except:
                            icon = None
                    
                    if icon:
                        print(f"{display_name}\x00info\x1f{url}\x1ficon\x1f{cache_icon(icon)}")
                    else:
                        print(f"{display_name}\x00info\x1f{url}")


# Function to detect and launch the appropriate Firefox command
def get_firefox_command():
    """Detect whether to use flatpak or native Firefox"""
    # Check if we're using flatpak Firefox (based on the directory structure we're reading from)
    if firefox_dir.exists() and '.var/app/org.mozilla.firefox' in str(firefox_dir):
        return ["flatpak", "run", "org.mozilla.firefox"]
    else:
        return ["firefox"]

if __name__ == "__main__":
    parser = ArgumentParser(description="generate list of bookmarks with icons for rofi")
    parser.add_argument('path',              default="",    nargs='?',      help="restrict list to a bookmark folder")
    parser.add_argument('-s', '--separator', default=" / ", metavar='sep',  help="seperator for paths")
    parser.add_argument('-p', '--profile',                  metavar='prof', help="firefox profile to use")
    args, _ = parser.parse_known_args()   # rofi gives us selected entry as additional argument -> ignore (not useful)

    if environ.get('ROFI_RETV') == '1':
        firefox_cmd = get_firefox_command()
        prof = [] if args.profile is None else ["-P", args.profile]
        subprocess.Popen(firefox_cmd + [environ['ROFI_INFO']] + prof, close_fds=True, start_new_session=True, stdout=subprocess.DEVNULL)
    else:
        try:
            search_path = [i for i in args.path.split('/') if i != '']
            profile_path = default_profile_path() if args.profile is None else path_from_name(args.profile)

            print("\x00prompt")  # change prompt
            write_rofi_input(profile_path, search_path=search_path, sep=args.separator)
        except Exception as e:
            print(f"Error: {e}")
            exit(1)


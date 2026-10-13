"""
List WebDAV collections with PROPFIND and transfer files over HTTP(S).

Many private servers (Nextcloud, ownCloud, iRODS davrods such as CyVerse's
authenticated /dav/, Apache mod_dav) answer WebDAV requests but serve no
browsable HTML index. A Depth: 1 PROPFIND lists each collection with exact
sizes and modification times, and checksums where the server keeps them.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from collections import deque
from email.utils import parsedate_to_datetime

import requests

from ..utils import CHECKSUM_ALGORITHMS_BY_STRENGTH, is_valid_checksum
from .base import CapabilityError, DownloadError, RemoteEntry
from .http import HttpBackend
from .index import _redirected_directory, _resolve_listing_link

DAV = "{DAV:}"
OWNCLOUD = "{http://owncloud.org/ns}"
PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/><oc:checksums/>"
    b"</d:prop></d:propfind>")
_OK = re.compile(r"\s200(\s|$)")


def _strongest_checksum(element):
    """Pick the strongest supported digest from an ``oc:checksums`` property."""
    if element is None:
        return None, None
    found = {}
    for text in element.itertext():
        for token in text.split():
            name, _, digest = token.partition(":")
            algorithm = name.lower()
            if is_valid_checksum(algorithm, digest):
                found[algorithm] = digest.lower()
    for algorithm in CHECKSUM_ALGORITHMS_BY_STRENGTH:
        if algorithm in found:
            return algorithm, found[algorithm]
    return None, None


def _timestamp(element):
    try:
        return int(parsedate_to_datetime(element.text.strip()).timestamp())
    except (AttributeError, TypeError, ValueError, IndexError):
        return None


class WebDavBackend(HttpBackend):
    """Discover WebDAV collections and transfer their files over HTTP(S)."""

    def iter_entries(self, on_directory=None):
        """Yield metadata from Depth: 1 PROPFIND listings, collection by collection."""
        queue, visited = deque([""]), set()
        while queue:
            directory = queue.popleft()
            if directory in visited:
                continue
            visited.add(directory)
            self.context.check_cancelled()
            body = self._propfind(directory)
            canonical = _redirected_directory(self.context, directory)
            if canonical != directory and canonical in visited:
                continue
            visited.add(canonical)
            for path, is_directory, size, mtime, checksum in self._parse_multistatus(
                    body, canonical):
                if is_directory:
                    if path not in visited:
                        queue.append(path)
                else:
                    algorithm, digest = checksum
                    yield RemoteEntry(
                        path=path, size=size, mtime=mtime,
                        checksum_algorithm=algorithm, checksum=digest,
                        checksum_source="server" if digest else None)
            if on_directory is not None:
                on_directory(canonical)

    def _propfind(self, directory):
        """Request one collection's members within the metadata size limit."""
        content = bytearray()
        limit = self.context.large_text_limit
        try:
            response, final_url = self._open_metadata_response(
                directory, method="PROPFIND", data=PROPFIND_BODY,
                headers={"Depth": "1",
                         "Content-Type": 'application/xml; charset="utf-8"'})
            with response:
                if response.status_code in {405, 501}:
                    raise CapabilityError(
                        "This URL does not answer WebDAV listing requests; "
                        "use --backend http for a browsable index")
                response.raise_for_status()
                if response.status_code != 207:
                    raise CapabilityError("This URL returned no WebDAV listing")
                for chunk in response.iter_content(chunk_size=8192):
                    self.context.check_cancelled()
                    content.extend(chunk)
                    if len(content) > limit:
                        raise DownloadError("WebDAV listing exceeds its size limit")
        except requests.RequestException as exc:
            raise self._download_error(exc) from None
        self.text_urls[directory] = final_url
        return bytes(content)

    def _parse_multistatus(self, body, directory):
        """Yield ``(path, is_directory, size, mtime, checksum)`` for each member."""
        # Entity declarations can expand without bound or read local files.
        if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
            raise DownloadError("WebDAV listing declares a document type")
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            raise DownloadError("WebDAV listing is not valid XML") from None
        for response in root.iter(f"{DAV}response"):
            href = response.findtext(f"{DAV}href")
            if not href:
                continue
            props = {}
            for propstat in response.findall(f"{DAV}propstat"):
                if not _OK.search(propstat.findtext(f"{DAV}status") or ""):
                    continue
                prop = propstat.find(f"{DAV}prop")
                if prop is not None:
                    props.update({child.tag: child for child in prop})
            kind = props.get(f"{DAV}resourcetype")
            is_collection = (kind is not None
                             and kind.find(f"{DAV}collection") is not None)
            link = _resolve_listing_link(
                self.context.remote.url, directory, href.strip(),
                "directory" if is_collection else "file")
            # The collection lists itself; links outside the root are ignored.
            if link is None or link[0] == directory:
                continue
            path, is_directory = link
            length = props.get(f"{DAV}getcontentlength")
            size = (int(length.text.strip()) if length is not None and length.text
                    and length.text.strip().isdigit() else None)
            yield (path, is_directory, None if is_directory else size,
                   _timestamp(props.get(f"{DAV}getlastmodified")),
                   _strongest_checksum(props.get(f"{OWNCLOUD}checksums")))

#!/usr/bin/env python3
import hashlib
import json
import re
import sys
from pathlib import Path


INDEX_MEDIA_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
IMAGE_MEDIA_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
REFERENCE_TYPE = "vnd.docker.reference.type"
REFERENCE_DIGEST = "vnd.docker.reference.digest"


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value)


def read_manifest(path, digest):
    if not valid_digest(digest):
        raise ValueError("Invalid image manifest digest")
    raw = Path(path).read_bytes()
    hashes = {
        "sha256:" + hashlib.sha256(data).hexdigest()
        for data in (raw, raw.removesuffix(b"\n"))
    }
    if digest not in hashes:
        raise ValueError("The registry manifest does not match its digest")
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("schemaVersion") != 2:
        raise ValueError("Invalid image manifest schema")
    return document


def index_entries(document):
    manifests = document.get("manifests")
    if document.get("mediaType") not in INDEX_MEDIA_TYPES or not isinstance(manifests, list):
        raise ValueError("Expected a container image index")
    images, attestations, seen = {}, {}, set()
    for item in manifests:
        if not isinstance(item, dict) or item.get("mediaType") not in IMAGE_MEDIA_TYPES:
            raise ValueError("The index contains an invalid image descriptor")
        digest = item.get("digest")
        platform, annotations = item.get("platform"), item.get("annotations", {})
        if (
            not valid_digest(digest)
            or digest in seen
            or not isinstance(platform, dict)
            or not isinstance(annotations, dict)
        ):
            raise ValueError("The index contains an invalid or repeated descriptor")
        seen.add(digest)
        system, architecture = platform.get("os"), platform.get("architecture")
        reference = annotations.get(REFERENCE_DIGEST)
        if REFERENCE_TYPE in annotations or REFERENCE_DIGEST in annotations:
            if (
                (system, architecture) != ("unknown", "unknown")
                or annotations.get(REFERENCE_TYPE) != "attestation-manifest"
                or not valid_digest(reference)
            ):
                raise ValueError("The index contains an invalid BuildKit attestation")
            attestations[digest] = reference
        elif system == "linux" and architecture in ("amd64", "arm64") and architecture not in images:
            images[architecture] = digest
        else:
            raise ValueError("The index contains an unknown or repeated runtime platform")
    if set(attestations.values()) - set(images.values()):
        raise ValueError("A BuildKit attestation does not refer to an image in the index")
    return images, attestations


def pushed_entries(path, digest, architecture):
    document = read_manifest(path, digest)
    if document.get("mediaType") in IMAGE_MEDIA_TYPES:
        return {architecture: digest}, {}
    images, attestations = index_entries(document)
    if set(images) != {architecture}:
        raise ValueError("The architecture push did not contain its expected runtime image")
    return images, attestations


def verify_index(amd64_digest, amd64_manifest, arm64_digest, arm64_manifest, index_digest, index_manifest):
    amd64_images, amd64_attestations = pushed_entries(amd64_manifest, amd64_digest, "amd64")
    arm64_images, arm64_attestations = pushed_entries(arm64_manifest, arm64_digest, "arm64")
    images, attestations = index_entries(read_manifest(index_manifest, index_digest))
    if images != (amd64_images | arm64_images):
        raise ValueError("The published index does not contain both pushed runtime images")
    if attestations != (amd64_attestations | arm64_attestations):
        raise ValueError("The published index does not retain the pushed BuildKit attestations")


if __name__ == "__main__":
    if len(sys.argv) != 7:
        sys.exit("Usage: verify-release-index.py AMD64_DIGEST AMD64_MANIFEST ARM64_DIGEST ARM64_MANIFEST INDEX_DIGEST INDEX_MANIFEST")
    try:
        verify_index(*sys.argv[1:])
    except (OSError, ValueError) as error:
        sys.exit(str(error))

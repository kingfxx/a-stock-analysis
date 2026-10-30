-- One row per physical source file. Collection records retain their original paths.
CREATE TABLE collection_source_archive_members (
 file_path TEXT PRIMARY KEY,
 archive_path TEXT NOT NULL,
 member_path TEXT NOT NULL,
 stored_sha256 TEXT NOT NULL,
 stored_bytes INTEGER NOT NULL CHECK(stored_bytes >= 0),
 archive_sha256 TEXT NOT NULL,
 archive_version INTEGER NOT NULL,
 archived_at TEXT NOT NULL,
 UNIQUE(archive_path, member_path)
);

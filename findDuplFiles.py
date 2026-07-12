"""
By wisaimtiac
   __ _         _ ___            _ _         _          
  / _(_)_ _  __| |   \ _  _ _ __| (_)__ __ _| |_ ___ ___
 |  _| | ' \/ _` | |) | || | '_ \ | / _/ _` |  _/ -_|_-<
 |_| |_|_||_\__,_|___/ \_,_| .__/_|_\__\__,_|\__\___/__/
                           |_| 
Duplicate File Finder & Cleaner
Finds byte-identical files and helps delete files interactively.

Strategy (cheap checks first, expensive ones only on what is left):
    1. Group by exact file size        -> a single stat() per file, no reads
    2. Sample-hash large groups        -> head + middle + tail (~768 KB)
    3. Full-hash of those left         -> the only stage that reads whole files
    (4.) Optional byte-for-byte verify -> --paranoid, defends against the
                                          astronomically unlikely hash collision

Files only ever get fully read if they share size & fingerprint with a file.
Examples:
    1) Keep newest in each group, delete the rest
            python findDuplicateFiles.py C:\ --auto
    
    2) Show exactly what --auto would remove, delete nothing
            python findDuplicateFiles.py C:\ --dry-run
"""
import os
import sys
import time
import hashlib
import argparse
import filecmp
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

CHUNK_SIZE = 1 << 20      # Read block for full hash.
SAMPLE_SIZE = 256 << 10   # Sample from head/middle/tail as fingerprint

SKIP_DIRS = { # Directories to be excluded on a Windows system drive.
    'windows', 'program files', 'program files (x86)', 'programdata',
    'system volume information', '$recycle.bin', 'appdata',
}

def new_hasher():
    return hashlib.blake2b(digest_size=16)


# --------------------------------------------------------------------------- #
#  Progress bar

class Progress:
    def __init__(self, total, label):
        self.total = total
        self.label = label
        self.n = 0
        self._last_draw = 0.0

    def tick(self, step=1):
        self.n += step
        now = time.monotonic()
        if now - self._last_draw >= 0.1 or self.n >= self.total:
            self._last_draw = now
            self._draw()

    def _draw(self):
        pct = int(self.n / self.total * 100) if self.total else 100
        filled = pct // 4
        bar = '█' * filled + '·' * (25 - filled)
        print(f"\r  {self.label:<13}[{bar}] {pct:3d}% "
              f"({self.n:,}/{self.total:,})", end="", flush=True)

    def done(self):
        self._draw()
        print()


# --------------------------------------------------------------------------- #
#  Hashing

def full_hash(path, _size=None):
    """Hashing the entire file in streamed blocks."""
    h = new_hasher()
    try:
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(CHUNK_SIZE), b''):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None

def sample_hash(path, size):
    """Cheap fingerprinting: hashing head, middle & tail in a single open()."""
    h = new_hasher()
    try:
        with open(path, 'rb') as f:
            h.update(f.read(SAMPLE_SIZE))                 # head
            if size > 2 * SAMPLE_SIZE:                    # mid (skip if tiny)
                f.seek(size // 2)
                h.update(f.read(SAMPLE_SIZE))
            if size > SAMPLE_SIZE:                        # tail
                f.seek(size - SAMPLE_SIZE)
                h.update(f.read(SAMPLE_SIZE))
        return h.hexdigest()
    except OSError:
        return None

def refine(groups, hash_fn, label, workers):
    """
    Taking groups of candidate-identical files, hashing each file, return only
    sub-groups where >1 file shares a hash.
    Hashing is parallelised because it is almost entirely I/O bound.
    """
    tasks = [(gid, p, s) for gid, g in enumerate(groups) for (p, s) in g]
    if not tasks:
        return []

    prog = Progress(len(tasks), label)
    buckets = defaultdict(list)

    def work(task):
        gid, path, size = task
        return gid, path, size, hash_fn(path, size)

    # Results consumed on main thread, so bucket dict & progress bar need no locking.
    if workers > 1 and len(tasks) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(work, tasks)
            for gid, path, size, digest in results:
                if digest is not None:
                    buckets[(gid, digest)].append((path, size))
                prog.tick()
    else:
        for task in tasks:
            gid, path, size, digest = work(task)
            if digest is not None:
                buckets[(gid, digest)].append((path, size))
            prog.tick()

    prog.done()
    return [items for items in buckets.values() if len(items) > 1]

def byte_verify(groups):
    """Final safety net: splitting each hash-group by true byte content."""
    verified = []
    for group in groups:
        clusters = []  # each cluster is list of confirmed-identical items
        for item in group:
            for cluster in clusters:
                if filecmp.cmp(cluster[0][0], item[0], shallow=False):
                    cluster.append(item)
                    break
            else:
                clusters.append([item])
        verified.extend(c for c in clusters if len(c) > 1)
    return verified


# --------------------------------------------------------------------------- #
#  Scanning

def iter_files(root):
    """
    Yield (path, size) for every regular file under `root`.
    Uses os.scandir so size comes from directory entry that has already
    been read instead of doing a second syscall via os.path.getsize.
    Symlinks are skipped, to not follow a cycle or double-count a target.
    """
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in SKIP_DIRS:
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            size = entry.stat(follow_symlinks=False).st_size
                            if size > 0:
                                yield entry.path, size
                    except OSError:
                        continue          # skip if unreadable entry
        except OSError:
            continue                      # skip if unreadable directory

def find_dupes(root, workers=4, paranoid=False):
    # --- Stage 1: group by size --------------------------------------------
    print(f"\n[1/3] Scanning '{root}' and grouping by size")
    sizes = defaultdict(list)
    scanned = 0
    last = 0.0
    for path, size in iter_files(root):
        sizes[size].append(path)
        scanned += 1
        now = time.monotonic()
        if now - last >= 0.1:
            last = now
            print(f"\r  -> Discovered {scanned:,} files", end="", flush=True)
    print(f"\r  -> Done. {scanned:,} files in {len(sizes):,} distinct sizes." + " " * 10)

    suspects = [[(p, size) for p in paths]
                for size, paths in sizes.items() if len(paths) > 1]
    if not suspects:
        return []

    small = [g for g in suspects if g[0][1] <= SAMPLE_SIZE]
    large = [g for g in suspects if g[0][1] > SAMPLE_SIZE]

    # --- Stage 2: fingerprinting large groups -----------------------------
    candidates = list(small)              # small files straight to full hashing
    if large:
        print("[2/3] Fingerprinting large files (head+middle+tail)")
        candidates += refine(large, sample_hash, "Sampling", workers)
    else:
        print("[2/3] No large files to fingerprint.")

    # --- Stage 3: confirm with a full hash ---------------------------------
    if not candidates:
        return []
    print("[3/3] Confirming with full-file hash")
    groups = refine(candidates, full_hash, "Hashing", workers)

    if paranoid and groups:
        print("  -> Verifying byte-for-byte")
        groups = byte_verify(groups)

    return groups


# --------------------------------------------------------------------------- #
#  Reporting & deletion

def human(n):
    size = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f"{size:.0f} {unit}" if unit == 'B' else f"{size:.1f} {unit}"
        size /= 1024


def interactive_delete(groups):
    if not groups:
        print("\nNo duplicate files found.")
        return

    reclaimable = sum((len(g) - 1) * g[0][1] for g in groups)
    file_map = {}      # id -> (path, size, group_index)
    group_count = defaultdict(int)
    next_id = 1

    print("\n" + "=" * 60)
    print(f"  {len(groups)} groups of duplicates  |  "
          f"reclaimable: {human(reclaimable)}")
    print("=" * 60)
    for gi, group in enumerate(groups):
        print(f"\n--- Group {gi + 1}  ({human(group[0][1])} each) ---")
        for path, size in group:
            file_map[next_id] = (path, size, gi)
            group_count[gi] += 1
            print(f" [{next_id}] {path}")
            next_id += 1

    print("\n" + "=" * 60)
    print("Enter the numbers to DELETE, separated by commas (e.g.: 2, 4)")
    print("One copy in each group must be kept.")
    print("Press Enter to cancel.")
    choice = input("\nNumbers to delete: ").strip()
    if not choice:
        print("Cancelled. No files deleted.")
        return

    selected = {int(x) for x in choice.replace(',', ' ').split() if x.isdigit()}
    selected &= file_map.keys()
    if not selected:
        print("No valid file numbers detected. Exiting.")
        return

    # Safety guard: never let the user wipe out every copy in a group.
    selected_per_group = defaultdict(int)
    for fid in selected:
        selected_per_group[file_map[fid][2]] += 1
    emptied = [gi + 1 for gi, n in selected_per_group.items()
               if n == group_count[gi]]
    if emptied:
        print(f"\nAborted: that would delete every copy "
              f"{', '.join(map(str, emptied))}. Keep at least one per group.")
        return

    freed = sum(file_map[fid][1] for fid in selected)
    print("\n" + "!" * 60)
    print(f"About to permanently delete {len(selected)} files "
          f"(~{human(freed)}).")
    print("!" * 60)
    if input("Type 'YES' to confirm: ") != 'YES':
        print("Deletion cancelled. Files are safe.")
        return

    deleted = 0
    reclaimed = 0
    print()
    for fid in sorted(selected):
        path, size, _ = file_map[fid]
        try:
            os.remove(path)
            deleted += 1
            reclaimed += size
            print(f"  [{deleted}/{len(selected)}] Deleted: {path}")
        except OSError as e:
            print(f"  [Error] {path}: {e}")
    print(f"\nDeleted {deleted}/{len(selected)} files, "
          f"freed {human(reclaimed)}.")


def auto_delete(groups, dry_run=False):
    """
    Non-interactive cleanup: in each duplicate group keep the most recently
    modified file, remove the rest.
    """
    if not groups:
        print("\nNo duplicate files found.")
        return

    def mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    mode = "DRY RUN (nothing will be removed)" if dry_run else "AUTO CLEANUP (keep newest)"
    print("\n" + "=" * 60)
    print(f"  {mode}  |  {len(groups)} duplicate group(s)")
    print("=" * 60)

    planned = deleted = freed = 0
    for gi, group in enumerate(groups, 1):
        # Newest by modification time; path as a deterministic tie-breaker.
        keeper, _ = max(group, key=lambda item: (mtime(item[0]), item[0]))
        print(f"\n--- Group {gi}  ({human(group[0][1])} each) ---")
        print(f"  KEEP     {keeper}")
        for path, size in group:
            if path == keeper:
                continue
            planned += 1
            if dry_run:
                print(f"  would rm {path}")
                freed += size
            else:
                try:
                    os.remove(path)
                    deleted += 1
                    freed += size
                    print(f"  deleted  {path}")
                except OSError as e:
                    print(f"  [Error]  {path}: {e}")

    print("\n" + "=" * 60)
    if dry_run:
        print(f"Dry run: {planned} file(s) would be deleted, "
              f"~{human(freed)} reclaimable. Re-run with --auto to apply.")
    else:
        print(f"Done: deleted {deleted}/{planned} file(s), freed {human(freed)}.")


# --------------------------------------------------------------------------- #
#  Entry point

def normalize_path(p):
    p = p.strip().strip('"').strip("'")
    if os.name == 'nt':
        if len(p) == 1 and p.isalpha():
            return f"{p.upper()}:\\"
        if len(p) == 2 and p[1] == ':':
            return p.upper() + "\\"
    return os.path.expanduser(p)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Find and remove duplicate files to free disk space.")
    parser.add_argument("path", nargs="?",
                        help="Drive or folder to scan (e.g. C:\\ or ~/Pictures)")
    parser.add_argument("-w", "--workers", type=int, default=None,
                        help="Parallel hashing threads. Default: scaled to CPUs. "
                             "Use 1 on a mechanical HDD.")
    parser.add_argument("--paranoid", action="store_true",
                        help="Add a final byte-for-byte comparison.")
    parser.add_argument("--auto", action="store_true",
                        help="Non-interactive: keep the newest file in each "
                             "group and delete the rest.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what --auto would delete without removing "
                             "anything.")
    args = parser.parse_args(argv)

    raw = args.path or input("Enter drive or folder (e.g. C:\\): ")
    root = normalize_path(raw)
    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a valid directory.")
        return 1

    workers = args.workers if args.workers else min(8, (os.cpu_count() or 2) * 2)

    print("--- Duplicate File Finder & Cleaner ---")
    start = time.time()
    groups = find_dupes(root, workers=workers, paranoid=args.paranoid)
    print(f"\nScan finished in {time.time() - start:.2f}s "
          f"using {workers} thread(s).")
    if args.auto or args.dry_run:
        auto_delete(groups, dry_run=args.dry_run)
    else:
        interactive_delete(groups)
    return 0

if __name__ == "__main__":
    sys.exit(main())

## findDuplicateFiles (fDF) v1.0.0
Finds byte-identical files in directory and helps you delete duplicates.

Requires Python 3.7+.

ATTENTION:	Tested and used on Windows 10 only!
Safe usage on other OS would need revision of list of directories to exclude in line 37 ff.

## Install
```bash
git clone https://github.com/wisaimtiac/findDuplFiles-v1_0_0.git
cd dupefinder
```
No dependencies to install.

## Usage
```bash
python dupefinder.py C:\\\\                    # interactive: review groups, pick what to delete
python dupefinder.py \\\~/Pictures --dry-run   # preview only, deletes nothing
python dupefinder.py /data --auto           # keep newest in each group, delete the rest
```
|Flag|Effect|
|-|-|
|`--auto`|Non-interactive; keeps the newest file per group.|
|`--dry-run`|Show the deletion plan, change nothing.|
|`--paranoid`|Add a final byte-for-byte comparison.|
|`--workers N`|Parallel hashing threads. Use `1` on a mechanical HDD.|

## 
## How it works
Progressively more expensive checks. File is fully read only if it survives all others:
1. **Group by size.** Files with a unique size can't have duplicates.
2. **Sample-hash** head, middle, and tail of large same-size files.
3. **Full hash** (BLAKE2b) of those left.
4. **Optional** byte-for-byte verification with `--paranoid`.

## Safety
* `--dry-run` previews without deleting.
* Interactive mode requires typing `YES` to confirm.
* Cannot delete the last copy in a group (structural guarantee).

## Author
wisaimtiac

## License
MIT

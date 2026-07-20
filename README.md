# FileFinderV2

Resolve prefixed game asset paths to Hash128 values and extract the matching
payloads from IDX/WPK archives.

Install dependencies:

```powershell
pip install -r requirements.txt
```

Example:

```powershell
python cli.py --game-root "S:\Loading Bay Games\Identity V" `
  "chr/player/dm65_survivor_w/h55_survivor_w_jyz/separate_dir/jyz_e_xiari/jyz_e_xiari.gim"
```

Interactive CLI:

```powershell
python cli.py
```

In interactive mode, press `Enter` to add the typed path into the queue and
press `Tab` to search and decompress the queued paths.

GUI:

```powershell
python gui.py
```

If `--game-root` is not provided, the CLI reads `user/memory.json`. When
`game_root` is empty, it first searches `[A-Z]:\Loading Bay Games\Identity V\dwrg.exe`.
If found, it prints the executable path and asks for `[Yes/No]` confirmation.
If the path is rejected or no match is found, it opens a `Game Executable`
file dialog filtered to `dwrg.exe`. The memory file stores only the selected
executable's directory path.

The CLI discovers archive prefixes from `.idx` files that exist in both
`res` and `Documents/res`. For example, `chr_player.idx` becomes the input
prefix `chr/player`. Extracted files are written to:

```text
outputs/<prefix>/<normalized_asset_path>
```

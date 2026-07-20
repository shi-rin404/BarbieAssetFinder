# FileFinderV2

Resolve prefixed game asset paths to Hash128 values and extract the matching
payloads from IDX/WPK archives.

Install dependencies:

```powershell
pip install -r requirements.txt
```

## GUI

```powershell
python gui.py
```

## Interactive CLI

```powershell
python cli.py
```

## CLI with arguments
- Split multiple inputs by space
- You might not use " since game pathes naturally doesn't include spaces

```powershell
python cli.py "chr/player/dm65_survivor_w/h55_survivor_w_jyz/separate_dir/jyz_e_xiari/jyz_e_xiari.gim" "chr/prop/h55_pendant_quanzhang/separate_dir/jyz_e_xiari/jyz_e_xiari_quanzhang.gim"
```



In interactive mode, press `Enter` to add the typed path into the queue and
press `Tab` to search and decompress the queued paths.




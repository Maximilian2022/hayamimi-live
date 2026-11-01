with open('scripts/launcher_gui.ps1', 'rb') as f:
    raw = f.read()

has_bom = raw.startswith(b'\xef\xbb\xbf')
text = raw.decode('utf-8')

text = text.replace('$script:AppVersion = "1.6.3"', '$script:AppVersion = "1.6.4"')
text = text.replace('(GUI v1.6.3)', '(GUI v1.6.4)')
text = text.replace('Text="GUI v1.6.3"', 'Text="GUI v1.6.4"')

out_bytes = (b'\xef\xbb\xbf' if has_bom else b'') + text.encode('utf-8')
with open('scripts/launcher_gui.ps1', 'wb') as f:
    f.write(out_bytes)

print("Updated launcher_gui.ps1 to v1.6.4 with BOM")


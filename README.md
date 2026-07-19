# waydroid-discord-rpc

![example rich presence](demo.png)

a dirty (vibe coded) implementation because I thought "wouldn't it be cool if waydroid had rich presence status just like bluestacks?". and turns out there were zero implementations online. (someone should actually make a proper implementation of this)

I have reviewed the code and after a few rounds of testing it seems stable enough. If you do run this, you should practice proper opsec and read the scripts to ensure it's safe

Read [this ai generated slop readme](ARCHITECTURE.md) if you want to understand the architecture of the script

tl;dr
- gets the current focused app process using `sudo waydroid shell dumpsys window` which is a package id
- looks through `waydroid app list` to get the package name of the said id
- sets your discord presence using pypresence with the variables obtained

These scripts assumes you already have [Waydroid](https://waydro.id) installed and configured.

For this to work you need both python-yaml and pypresence installed globally. To install these, refer to your package manager's instructions. On Arch based distros (cuz I use Arch), the command is: `sudo pacman -Syu --needed python-yaml python-pypresence`

Clone this repository first (or just download as a zip, whatever works):

```
git clone https://github.com/MiKl-LS/waydroid-discord-rpc
cd discord-waydroid-rpc
```

To install the script run: 

```sudo ./install.sh```

then edit the config file with your favorite editor

```
sudo nano /etc/waydroid-rpc/config.yaml  
```
Set the `discord_client_id:` to the Client ID of your custom Discord application

To obtain this, do the following (blatantly copied from config.yaml)

   1. Go to https://discord.com/developers/applications
   2. New Application → give it a name (e.g. "Waydroid")
   3. Copy its Client ID from the General Information page

Additonally, you can upload one art asset for the rich presence icon (the default key is "waydroid") although it is not required 

Then enable the two systemd services using systemctl:
(two are needed because `waydroid shell` needs root and pypresence only works for the current user)

```
sudo systemctl enable --now waydroid-rpc-root.service
systemctl --user enable --now waydroid-rpc-user.service
```

You can verify if the processes are running with:

```
sudo systemctl status waydroid-rpc-root.service
systemctl --user status waydroid-rpc-user.service
```

When you open a waydroid application, it should set your rpc accordingly.

To uninstall this script, run:

```sudo ./uninstall.sh```

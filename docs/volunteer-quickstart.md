# How to run the channel-occupancy survey

**You need:** an RTL-SDR dongle (the ~$30 kind), any antenna, and a computer
that can be left running for a few hours. No radio knowledge required.

---

## What this program records, and what it does not

It records, for each channel, **what fraction of the time a carrier is
present** — a number like "busy 14% of the hour" — plus the signal strength
as a number.

**It does not record audio. It does not decode or demodulate anything. It
cannot tell you what was said, or by whom.** It measures energy, the way a
light meter measures brightness without photographing the room. That is a
property of how it is built, not a promise: the only thing the radio code
hands to the rest of the program is a list of signal strengths. There is no
code path that could produce a recording.

**Location is optional and deliberately coarse.** If you fill in a grid
square it is 4 characters, which covers about 100 × 160 km. The program
**refuses** anything more precise than 6 characters. Leave the line out and
your data is still useful.

---

## 1. Install

Install the `rtl_sdr` tools for your system:

- **macOS** — `brew install librtlsdr`
- **Debian / Ubuntu / Raspberry Pi** — `sudo apt install rtl-sdr`
- **Windows** — download the `rtl-sdr` release and put its folder on your PATH

Then install this program:

```bash
pip install occupancy-survey
```

Check the dongle is found:

```bash
rtl_test -t
```

If that prints a tuner name, you are ready. If it says "No supported devices
found", unplug the dongle, plug it back in, and try again.

---

## 2. Set up your config

Copy `config.volunteer.yaml` to `config.yaml` and edit the **three lines
marked EDIT ME**:

- `site_id` — any short name, like `portland-1`
- `antenna` — in plain words, like `rubber duck on a windowsill`
- `grid_square` — your 4-character grid square, **or delete the line**

Do not change anything else. The other settings are the same for every
volunteer so the results can be compared.

---

## 3. Check it before you start

```bash
occupancy-survey --config config.yaml --plan
```

This touches no hardware. It prints what the survey will do and what it
cannot see. If it prints an error about your config, fix that first.

Now a two-minute test run:

```bash
occupancy-survey --config config.yaml --duration 120
```

It should print the name of a file in `data/`. If it does, you are done
setting up.

---

## 4. Run it

```bash
occupancy-survey --config config.yaml
```

Leave it running. It writes one file per hour into `data/`. Press `Ctrl-C`
to stop; the hour in progress is finished and saved.

**Overnight is the minimum useful run.** A few hours is better. Quiet
channels need a lot of listening before "quiet" means anything — on an
RTL-SDR with these settings, a channel used for 3 seconds every 5 minutes is
only spotted about **half** the time in an hour. Busy channels settle down
much faster.

---

## 5. Did it work?

**The program tells you as it runs.** Once a minute it prints how many
channels it is hearing and how busy they are:

```
occupancy survey   portland-1   elapsed 00:04:00   0 file(s) written
  band          visits   chans  active   busiest
  vhf_lmr_low       12     417       5     151.4750 MHz  38.2%
  nwr                3       7       1     162.4750 MHz 100.0%   [reference]
  bcast_fm           6     100       9     101.3000 MHz 100.0%   [pass]
  17 channel(s) with a carrier across 4 band(s) visited
```

The **active** column is the one to watch. If it has numbers in it, and
`bcast_fm` says `[pass]`, it is working. If it stays empty for more
than a few minutes, move the antenna near a window and try again.

For more detail, open the newest file in `data/` — they are gzipped JSON,
so `gunzip -c data/occ_*.json.gz | head -40` will show the top.

Look for the `bcast_fm` band and its `control_check`. It should say
`"verdict": "pass"`.

- **`pass`** — good. An FM broadcast transmitter radiates a carrier
  continuously, even during quiet moments in the programme, so finding a
  local station at ~100% shows the receiver and the settings are working.
- **`fail`** — the numbers from this run are not trustworthy. Either no FM
  station reaches your antenna (unlikely), or something is wrong with the
  dongle, the antenna or the gain. Try moving the antenna near a window
  first.

Do **not** use the `nwr` band for this check. NOAA Weather Radio looks like
it should be a perfect reference — a 24/7 service — but whether its
transmitter keys on and off between programme cycles is not actually
established, and this survey is partly intended to find out. If `nwr` reads
40%, that may well be correct.

---

## 6. Send it in

Zip the `data/` folder and send it. That is the whole contribution.

Files from different volunteers merge automatically — the program subtracts
each receiver's own noise floor before writing, so your cheap dongle and
somebody's expensive receiver produce comparable numbers.

---

## Questions people ask

**Will this interfere with anything?** No. A receiver only listens.

**Can I use a different antenna / an upconverter / a better SDR?** Yes.
Describe it in the `antenna` line. Reach matters more than sensitivity here.

**Does it need internet?** No. It writes files locally and never uploads
anything.

**Is it legal?** Listening is. This records only whether a carrier was
present and how strong it was, which is why it stays well clear of the
rules about intercepting communications — there is no content to intercept.
If you are unsure about your own jurisdiction, ask before running it.

**It stopped with an error about gain.** Do not set `gain: 0`. Zero means
automatic, which breaks the measurement. Use 40.

---

## Seeing what it looks like, before you have a dongle

```bash
occupancy-survey --config config.demo.yaml --duration 120 --status-interval 5
```

No hardware needed. It simulates a few carriers so you can see the display
working and check your install. Everything it writes is marked `synthetic`
and is not a measurement of anything.

# Publishing the SIP Phone extension to the Chrome Web Store

Everything referenced here is in this folder (`webstore/`) of the
[sip-phone](https://github.com/ilandau-demo/sip-phone) repository.

| File | Use |
|---|---|
| `sip-phone-extension-2.4.5-webstore.zip` | the package you upload |
| `screenshot-1-phone-1280x800.png`, `screenshot-2-presence-1280x800.png` | listing screenshots |
| `promo-tile-440x280.png` | optional promo tile |
| `LISTING.txt` | text to paste into the store forms |
| `privacy-policy.md` | the privacy policy (published at the URL in step 1) |

## 1. Privacy policy URL (once)
The store needs a page anyone can open without signing in.
Repository **https://github.com/ilandau-demo/sip-phone-privacy** holds the
policy as its README; the repository must be **public** (Settings → Danger
zone → Change visibility → Public). The URL to give the store is:

    https://github.com/ilandau-demo/sip-phone-privacy

## 2. Developer account (once)
1. https://chrome.google.com/webstore/devconsole — sign in with Google.
2. Accept the developer agreement, pay the one-time $5 fee, verify the email.

## 3. Upload and fill in the listing (about 15 minutes)
1. **+ New item** → upload `sip-phone-extension-2.4.5-webstore.zip`.
2. **Store listing** tab (copy from `LISTING.txt`): Name, Summary, Description;
   category *Productivity → Communication*; language *English (US)*; upload
   the two screenshots.
3. **Privacy** tab (copy from `LISTING.txt`): single purpose; one justification
   per permission; *remote code: No*; data usage — tick *Authentication
   information* and *Personal communications*, certify the three "not used
   for…" boxes; privacy policy URL from step 1.
4. **Distribution** tab: visibility **Unlisted**; leave the rest.
5. **Submit for review.** Expect an email in 1–3 business days.

## 4. After approval (once)
1. Copy the item's 32-letter ID (the end of the store link
   `…/detail/sip-phone/XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX`).
2. SIP Phone → **Settings → Browser extension → Chrome Web Store extension ID**
   → paste → Save. The app now accepts the store copy.
3. In this repo, set `$StoreUrl` near the top of `installer/install.ps1` to the
   store link and commit; Install.bat then sends new users to the store and
   records the ID for them.
4. Share the store link. *Add to Chrome* → pin SIP Phone. Presence-only users
   are done; phone users also run Install.bat from
   `releases/2.4.5/SIP-Phone-2.4.5.zip`.

## Updating the extension later
```
python -c "from sipphone import extension_sync; open('store.zip','wb').write(extension_sync.zip_bytes(keep_key=False))"
```
Dashboard → your item → **Package** → *Upload new package* → Submit. Chrome
updates everyone automatically within a few hours.

## If a reviewer asks
- *Why `https://*/*` as an optional permission?* The user's phone system is on
  a customer-specific host name; the permission is requested only for the one
  address the user types in team-presence mode, never silently.
- *Why `nativeMessaging`?* The extension is a front end for the SIP Phone desktop
  app on the same computer; nothing is sent to any other server.

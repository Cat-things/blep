/*
 * Site configuration. Every page reads its links from here, so a self-hosted
 * copy only has to change this one file.
 *
 *   clientId     your bot's application ID (Discord Developer Portal > General Information)
 *   support      invite link to your support server (/discord redirects here)
 *   docs         documentation site
 *   github       source code
 *
 * Optional keys: permissions (defaults to "8", Administrator) and
 * selfHosting (defaults to `${docs}/self-hosting/installation`).
 */
window.BLEP_CONFIG = {
  clientId: "YOUR_CLIENT_ID",
  support: "https://discord.gg/blep",
  docs: "https://docs.blep.wtf",
  github: "https://github.com/cat-things/blep",
};

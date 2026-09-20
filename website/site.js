const copyButton = document.querySelector('[data-copy-install]');
const installCommand = document.querySelector('#install-command');

if (copyButton && installCommand && navigator.clipboard) {
  copyButton.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(installCommand.textContent.trim());
      copyButton.textContent = 'COPIED ✓';
      window.setTimeout(() => { copyButton.textContent = 'COPY COMMAND'; }, 2200);
    } catch {
      copyButton.textContent = 'SELECT COMMAND ABOVE';
    }
  });
} else if (copyButton) {
  copyButton.hidden = true;
}

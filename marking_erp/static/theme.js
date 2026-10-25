(() => {
  let theme;
  try { theme = localStorage.getItem('erp-theme'); } catch (_) {}
  document.documentElement.dataset.theme = ['light', 'dark'].includes(theme) ? theme : (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
})();

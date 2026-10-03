// Progressive enhancement only: every page is fully readable without JavaScript.

const menuToggle = document.querySelector('.menu-toggle');
const menu = document.querySelector('#site-menu');

if (menuToggle && menu) {
  menuToggle.addEventListener('click', () => {
    const isOpen = menu.classList.toggle('open');
    menuToggle.setAttribute('aria-expanded', String(isOpen));
  });
  menu.querySelectorAll('a').forEach((link) => {
    link.addEventListener('click', () => {
      menu.classList.remove('open');
      menuToggle.setAttribute('aria-expanded', 'false');
    });
  });
}

// Hero preview: Body / Timeline / Assertions tabs.
const mockTabs = document.querySelectorAll('.mock-tab');
mockTabs.forEach((tab) => {
  tab.addEventListener('click', () => {
    mockTabs.forEach((other) => {
      const selected = other === tab;
      other.setAttribute('aria-selected', String(selected));
      other.tabIndex = selected ? 0 : -1;
      const panel = document.getElementById(other.getAttribute('aria-controls'));
      if (panel) panel.hidden = !selected;
    });
  });
  tab.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
    const list = Array.from(mockTabs);
    const step = event.key === 'ArrowRight' ? 1 : -1;
    const next = list[(list.indexOf(tab) + step + list.length) % list.length];
    next.click();
    next.focus();
  });
});

// Workspace accordion on the dark band: one item open at a time.
const capabilities = document.querySelectorAll('.capability');
capabilities.forEach((item) => {
  item.addEventListener('click', () => {
    capabilities.forEach((other) => other.setAttribute('aria-expanded', String(other === item)));
  });
});

// Highlight the documentation section currently in view.
const docsLinks = document.querySelectorAll('.docs-nav a');
const docsSections = document.querySelectorAll('.docs-body section');

if (docsLinks.length && docsSections.length && 'IntersectionObserver' in window) {
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        docsLinks.forEach((link) => {
          link.classList.toggle('active', link.getAttribute('href') === `#${entry.target.id}`);
        });
      });
    },
    { rootMargin: '-100px 0px -70% 0px' }
  );
  docsSections.forEach((section) => observer.observe(section));
}

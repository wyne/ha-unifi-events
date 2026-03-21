const VERSION = '5';

class RefreshableTapPictureCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._interval = null;
    this._cells = [];   // [{img, label}] — built once, patched on each fetch
    this._urls = [];   // current img URLs, used to diff on next fetch
  }

  setConfig(config) {
    if (!config.url) throw new Error('You must define a url');
    this._config = config;
    this._build();
  }

  set hass(h) {
    const entity = this._config?.entity;
    if (entity) {
      const prev = this._hass?.states[entity]?.state;
      const next = h.states[entity]?.state;
      if (next && next !== prev) this._fetchAndUpdate('state update');
    }
    this._hass = h;
  }

  _typeIcon(type) {
    const icons = {
      person: '<svg viewBox="0 0 24 24" fill="#555"><circle cx="12" cy="7" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/></svg>',
      vehicle: '<svg viewBox="0 0 24 24" fill="#555"><rect x="2" y="10" width="20" height="8" rx="2"/><path d="M5 10l3-5h8l3 5"/><circle cx="7" cy="18" r="2"/><circle cx="17" cy="18" r="2"/></svg>',
      animal: '<svg viewBox="0 0 24 24" fill="#555"><ellipse cx="12" cy="13" rx="5" ry="4"/><circle cx="7" cy="8" r="2"/><circle cx="17" cy="8" r="2"/><circle cx="5" cy="13" r="1.5"/><circle cx="19" cy="13" r="1.5"/><circle cx="12" cy="19" r="1.5"/></svg>',
      package: '<svg viewBox="0 0 24 24" fill="#555"><rect x="3" y="8" width="18" height="13" rx="1"/><path d="M3 8l3-5h12l3 5"/><line x1="12" y1="8" x2="12" y2="21" stroke="#333" stroke-width="1.5"/></svg>',
    };
    return icons[type] || '<svg viewBox="0 0 24 24" fill="#555"><circle cx="12" cy="12" r="9"/></svg>';
  }

  _fuzzyAge(isoTs) {
    const seconds = Math.floor((Date.now() - new Date(isoTs)) / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(seconds / 86400);
    if (seconds < 60) return 'now';
    if (minutes < 60) return `${minutes} m`;
    if (hours < 24) return `${hours} h`;
    if (days < 7) return `${days} d`;
    return `${Math.floor(days / 7)} w`;
  }

  _build() {
    const cols = this._config.cols || 3;
    const count = this._config.count || 3;
    const lightboxCount = this._config.lightbox_count || 6;
    const refreshInterval = (this._config.refresh_interval || 300) * 1000;

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }

        .card {
          position: relative;
          width: 100%;
          border-radius: var(--ha-card-border-radius, 12px);
          overflow: hidden;
          background: #000;
          box-shadow: var(--ha-card-box-shadow, none);
          cursor: zoom-in;
        }

        .grid {
          display: grid;
          grid-template-columns: repeat(${cols}, 1fr);
          gap: 4px;
        }

        .cell {
          position: relative;
          aspect-ratio: 1 / 1;
          background: #111;
          overflow: hidden;
        }

        .cell img {
          display: block;
          width: 100%;
          height: 100%;
          object-fit: cover;
        }

        .placeholder {
          display: none;
          width: 100%;
          height: 100%;
          align-items: center;
          justify-content: center;
          background: #1a1a1a;
        }

        .placeholder svg {
          width: 48px;
          height: 48px;
        }

        .cell .label {
          position: absolute;
          bottom: 6px;
          left: 6px;
          background: rgba(0, 0, 0, 0.65);
          color: #fff;
          font-size: 13px;
          font-family: sans-serif;
          padding: 2px 6px;
          border-radius: 4px;
          pointer-events: none;
        }

        .lightbox {
          display: none;
          position: fixed;
          inset: 0;
          z-index: 9999;
          background: rgba(0, 0, 0, 0.92);
          align-items: center;
          justify-content: center;
          cursor: zoom-out;
        }
        .lightbox.open { display: flex; }

        .lightbox-inner {
          overflow: hidden;
        }

        .lightbox .grid {
          grid-template-columns: repeat(${cols}, 1fr);
        }

        .lightbox .cell {
          aspect-ratio: 1 / 1;
        }

        .close-btn {
          position: fixed;
          top: 16px;
          right: 20px;
          color: #fff;
          font-size: 32px;
          line-height: 1;
          cursor: pointer;
          opacity: 0.7;
          font-family: sans-serif;
          user-select: none;
          z-index: 10000;
        }
        .close-btn:hover { opacity: 1; }

        .version {
          position: absolute;
          top: 4px;
          left: 6px;
          color: #ededed;
          text-shadow: 0px 1px 2px #000000;
          font-size: 16px;
          font-weight: bold;
          font-family: monospace;
          pointer-events: none;
        }
      </style>

      <div class="card" id="card">
        <div class="grid" id="grid"></div>
        <span class="version">v${VERSION}</span>
      </div>

      <div class="lightbox" id="lightbox">
        <span class="close-btn" id="close-btn">&times;</span>
        <div class="lightbox-inner">
          <div class="grid" id="lightbox-grid"></div>
        </div>
      </div>
    `;

    const card = this.shadowRoot.getElementById('card');
    const lightbox = this.shadowRoot.getElementById('lightbox');
    const closeBtn = this.shadowRoot.getElementById('close-btn');
    const grid = this.shadowRoot.getElementById('grid');
    const lightboxGrid = this.shadowRoot.getElementById('lightbox-grid');
    const lightboxInner = this.shadowRoot.querySelector('.lightbox-inner');

    // Fit the lightbox grid within the viewport without scrolling.
    // Cells are square, so grid aspect ratio = cols : rows.
    // Width is capped at whichever limit is hit first: 95vw or the width
    // that makes the full grid height equal 95vh.
    const rows = Math.ceil(lightboxCount / cols);
    lightboxInner.style.width = `min(95vw, calc(95vh * ${cols} / ${rows}))`;

    // Pre-build empty cells for the main grid
    this._cells = [];
    for (let i = 0; i < count; i++) {
      const cell = document.createElement('div');
      cell.className = 'cell';
      const img = document.createElement('img');
      img.decoding = 'async';
      const placeholder = document.createElement('div');
      placeholder.className = 'placeholder';
      const label = document.createElement('span');
      label.className = 'label';
      cell.appendChild(img);
      cell.appendChild(placeholder);
      cell.appendChild(label);
      grid.appendChild(cell);
      this._cells.push({ img, placeholder, label });
    }

    // Pre-build empty cells for the lightbox grid
    this._lightboxCells = [];
    for (let i = 0; i < lightboxCount; i++) {
      const cell = document.createElement('div');
      cell.className = 'cell';
      const img = document.createElement('img');
      img.decoding = 'async';
      const placeholder = document.createElement('div');
      placeholder.className = 'placeholder';
      const label = document.createElement('span');
      label.className = 'label';
      cell.appendChild(img);
      cell.appendChild(placeholder);
      cell.appendChild(label);
      lightboxGrid.appendChild(cell);
      this._lightboxCells.push({ img, placeholder, label });
    }

    // Lightbox open/close
    card.addEventListener('click', () => lightbox.classList.add('open'));
    lightbox.addEventListener('click', () => lightbox.classList.remove('open'));
    closeBtn.addEventListener('click', () => lightbox.classList.remove('open'));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') lightbox.classList.remove('open');
    });

    // Initial fetch + start interval
    this._fetchAndUpdate('initial');
    if (this._interval) clearInterval(this._interval);
    this._interval = setInterval(() => this._fetchAndUpdate('interval'), refreshInterval);
  }

  _fetchAndUpdate(reason = 'interval') {
    const count = this._config.count || 3;
    const lightboxCount = this._config.lightbox_count || 6;
    const url = this._config.url;

    console.debug(`[rtp-card] fetching (${reason})`, url);
    fetch(`${url}?_t=${Date.now()}`, { cache: 'no-store' })
      .then(r => r.json())
      .then(data => {
        const thumbs = data.thumbnails || [];
        this._patch(this._cells, thumbs.slice(0, count).reverse());
        this._patch(this._lightboxCells, thumbs.slice(0, lightboxCount).reverse());
      })
      .catch(() => { /* ignore fetch errors — stale display is fine */ });
  }

  _patch(cells, thumbs) {
    cells.forEach((cell, i) => {
      const thumb = thumbs[i];
      if (!thumb) {
        cell.img.removeAttribute('src');
        cell.label.textContent = '';
        return;
      }
      if (!thumb.url) {
        cell.img.removeAttribute('src');
        cell.img.style.display = 'none';
        cell.placeholder.innerHTML = this._typeIcon(thumb.type);
        cell.placeholder.style.display = 'flex';
      } else {
        cell.placeholder.style.display = 'none';
        cell.img.style.display = 'block';
        // Only update src if URL changed — avoids unnecessary decode
        if (cell.img.getAttribute('src') !== thumb.url) {
          cell.img.setAttribute('src', thumb.url);
        }
      }
      cell.label.textContent = this._fuzzyAge(thumb.ts);
      cell.label.dataset.ts = thumb.ts;
    });
  }

  connectedCallback() {
    // Restart the interval if the element is reconnected to the DOM after
    // being removed (HA does this during rendering and view navigation).
    if (this._config && !this._interval) {
      const refreshInterval = (this._config.refresh_interval || 30) * 1000;
      this._fetchAndUpdate();
      this._interval = setInterval(() => this._fetchAndUpdate(), refreshInterval);
    }
  }

  disconnectedCallback() {
    if (this._interval) clearInterval(this._interval);
    this._interval = null;
  }

  getCardSize() {
    return 3;
  }
}

customElements.define('refreshable-tap-picture-card', RefreshableTapPictureCard);

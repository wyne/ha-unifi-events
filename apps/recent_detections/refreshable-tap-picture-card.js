class RefreshableTapPictureCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._interval = null;
    this._cells    = [];   // [{img, label}] — built once, patched on each fetch
    this._urls     = [];   // current img URLs, used to diff on next fetch
  }

  setConfig(config) {
    if (!config.url) throw new Error('You must define a url');
    this._config = config;
    this._build();
  }

  _fuzzyAge(isoTs) {
    const seconds = Math.floor((Date.now() - new Date(isoTs)) / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours   = Math.floor(minutes / 60);
    const days    = Math.floor(seconds / 86400);
    if (seconds < 60) return 'now';
    if (minutes < 60) return `${minutes} m`;
    if (hours   < 24) return `${hours} h`;
    if (days    <  7) return `${days} d`;
    return `${Math.floor(days / 7)} w`;
  }

  _build() {
    const cols             = this._config.cols             || 3;
    const count            = this._config.count            || 3;
    const lightboxCount    = this._config.lightbox_count   || 6;
    const refreshInterval  = (this._config.refresh_interval || 30) * 1000;

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
      </style>

      <div class="card" id="card">
        <div class="grid" id="grid"></div>
      </div>

      <div class="lightbox" id="lightbox">
        <span class="close-btn" id="close-btn">&times;</span>
        <div class="lightbox-inner">
          <div class="grid" id="lightbox-grid"></div>
        </div>
      </div>
    `;

    const card          = this.shadowRoot.getElementById('card');
    const lightbox      = this.shadowRoot.getElementById('lightbox');
    const closeBtn      = this.shadowRoot.getElementById('close-btn');
    const grid          = this.shadowRoot.getElementById('grid');
    const lightboxGrid  = this.shadowRoot.getElementById('lightbox-grid');
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
      const cell  = document.createElement('div');
      cell.className = 'cell';
      const img   = document.createElement('img');
      img.decoding = 'async';
      const label = document.createElement('span');
      label.className = 'label';
      cell.appendChild(img);
      cell.appendChild(label);
      grid.appendChild(cell);
      this._cells.push({ img, label });
    }

    // Pre-build empty cells for the lightbox grid
    this._lightboxCells = [];
    for (let i = 0; i < lightboxCount; i++) {
      const cell  = document.createElement('div');
      cell.className = 'cell';
      const img   = document.createElement('img');
      img.decoding = 'async';
      const label = document.createElement('span');
      label.className = 'label';
      cell.appendChild(img);
      cell.appendChild(label);
      lightboxGrid.appendChild(cell);
      this._lightboxCells.push({ img, label });
    }

    // Lightbox open/close
    card.addEventListener('click', () => lightbox.classList.add('open'));
    lightbox.addEventListener('click', () => lightbox.classList.remove('open'));
    closeBtn.addEventListener('click', () => lightbox.classList.remove('open'));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') lightbox.classList.remove('open');
    });

    // Initial fetch + start interval
    this._fetchAndUpdate();
    if (this._interval) clearInterval(this._interval);
    this._interval = setInterval(() => this._fetchAndUpdate(), refreshInterval);
  }

  _fetchAndUpdate() {
    const count         = this._config.count          || 3;
    const lightboxCount = this._config.lightbox_count || 6;
    const url           = this._config.url;

    fetch(`${url}?_t=${Date.now()}`)
      .then(r => r.json())
      .then(data => {
        const thumbs = data.thumbnails || [];
        this._patch(this._cells,        thumbs.slice(0, count));
        this._patch(this._lightboxCells, thumbs.slice(0, lightboxCount));
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
      // Only update src if URL changed — avoids unnecessary decode
      if (cell.img.getAttribute('src') !== thumb.url) {
        cell.img.setAttribute('src', thumb.url);
      }
      cell.label.textContent = this._fuzzyAge(thumb.ts);
      cell.label.dataset.ts  = thumb.ts;
    });
  }

  disconnectedCallback() {
    if (this._interval) clearInterval(this._interval);
  }

  getCardSize() {
    return 3;
  }
}

customElements.define('refreshable-tap-picture-card', RefreshableTapPictureCard);

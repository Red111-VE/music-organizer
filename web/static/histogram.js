/* ===========================================================================
 * histogram.js — utilidades client-side para el histograma de energía.
 *
 * Expone ``window.Histogram`` con funciones puras (sin estado, sin DOM).
 * results.html las consume desde Alpine; no toca el server.
 *
 * Convenciones de color: ``LEVEL_COLOR`` reusa el spectrum frío→cálido que
 * progress.html y app.css ya usan para los niveles BAJA/MEDIA/ALTA/MUY ALTA.
 * Mantener sincronizado con el ``[data-level]`` del CSS si cambia uno.
 *
 * Cargado SIN ``defer`` desde results.html (durante el parseo del body), así
 * ``window.Histogram`` existe antes de que Alpine (con ``defer``) inicialice
 * el x-data y llame a ``resultsView()``.
 * =========================================================================== */

(function () {
  'use strict';

  const LEVELS = ['BAJA', 'MEDIA', 'ALTA', 'MUY ALTA'];

  /* Espectro frío→cálido (BAJA azul → MUY ALTA roja). Mismo orden que en
     el [data-level] de app.css. Si cambiás uno, sincronizá el otro. */
  const LEVEL_COLOR = {
    'BAJA':     'rgba( 80, 130, 240, 0.70)',
    'MEDIA':    'rgba(180, 120, 220, 0.70)',
    'ALTA':     'rgba(255, 130,  80, 0.75)',
    'MUY ALTA': 'rgba(255,  45,  45, 0.85)',
  };

  /* Defaults del proyecto (core.config.DEFAULT_ENERGY_THRESHOLDS). */
  const DEFAULT_THRESHOLDS = [4.8, 5.8, 6.6];


  /* -------------------------------------------------------------------------
   * Bucketing — espejo exacto de core.analyzer.energy_bucket.
   * Comparamos contra ``< low / < med / < high``, NO ``<=``: paridad con
   * Python para que el conteo aquí matchee el del server.
   * --------------------------------------------------------------------- */
  function bucketize(arousal, thresholds) {
    const [lo, md, hi] = thresholds;
    if (arousal < lo) return 'BAJA';
    if (arousal < md) return 'MEDIA';
    if (arousal < hi) return 'ALTA';
    return 'MUY ALTA';
  }


  /* -------------------------------------------------------------------------
   * Bins del histograma — N bins uniformes sobre [min, max].
   * Devuelve [{count, start, end}], útil para renderizar las barras y
   * colorearlas por bucket (el caller decide qué color usar según el
   * centro del bin y los thresholds vigentes).
   * --------------------------------------------------------------------- */
  function computeBins(arousals, binCount, min, max) {
    const out = new Array(binCount);
    const width = (max - min) / binCount;
    for (let i = 0; i < binCount; i++) {
      out[i] = { count: 0, start: min + i * width, end: min + (i + 1) * width };
    }
    for (const a of arousals) {
      let i = Math.floor((a - min) / width);
      if (i < 0) i = 0;
      if (i >= binCount) i = binCount - 1;
      out[i].count++;
    }
    return out;
  }


  /* -------------------------------------------------------------------------
   * Cuartiles "exclusive" (paridad con statistics.quantiles(data, n=4) de
   * Python — método por defecto). Es lo que core.recalibrator usa para el
   * auto-calibrado. La fórmula:
   *
   *     pos = i * (N + 1) / n          (1-indexed, i=1..n-1)
   *
   * Para n=4 da 3 cortes Q1/Q2/Q3, interpolación lineal entre puntos
   * adyacentes. Difiere de numpy/percentile() en datasets chicos.
   *
   * Para que los valores que muestra "Auto-calibrar" coincidan con los que
   * generaría ``music-organizer recalibrate --auto-calibrate``, usamos
   * exactamente esta fórmula.
   * --------------------------------------------------------------------- */
  function quantilesExclusive(arr, n) {
    if (arr.length < 2) return [];
    const sorted = [...arr].sort((a, b) => a - b);
    const N = sorted.length;
    const result = [];
    for (let i = 1; i < n; i++) {
      const pos = (i * (N + 1)) / n;          // 1-indexed posición fraccionaria
      const k = Math.floor(pos);
      const frac = pos - k;
      let v;
      if (k < 1)       v = sorted[0];
      else if (k >= N) v = sorted[N - 1];
      else             v = sorted[k - 1] + frac * (sorted[k] - sorted[k - 1]);
      result.push(v);
    }
    return result;
  }


  /* Devuelve los 3 thresholds auto-calibrados a partir de la distribución
     real. Si no hay datos suficientes, vuelve a los defaults. Los valores
     se redondean a 1 decimal (mismo grano que el slider y los tags
     escritos por el analyzer). */
  function autoCalibrate(arousals) {
    if (arousals.length < 2) return [...DEFAULT_THRESHOLDS];
    const q = quantilesExclusive(arousals, 4);
    return q.map(v => Math.round(v * 10) / 10);
  }


  /* -------------------------------------------------------------------------
   * Distribución por nivel — { BAJA, MEDIA, ALTA, MUY ALTA } con conteos.
   * --------------------------------------------------------------------- */
  function levelDistribution(arousals, thresholds) {
    const out = { 'BAJA': 0, 'MEDIA': 0, 'ALTA': 0, 'MUY ALTA': 0 };
    for (const a of arousals) {
      out[bucketize(a, thresholds)]++;
    }
    return out;
  }


  /* -------------------------------------------------------------------------
   * Distribución por género — [[genre, count], ...] ordenada desc.
   * --------------------------------------------------------------------- */
  function genreDistribution(tracks) {
    const counts = new Map();
    for (const t of tracks) {
      const g = (t.genre || '').trim() || '—';
      counts.set(g, (counts.get(g) || 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }


  /* -------------------------------------------------------------------------
   * Export al global. results.html lo consume vía Alpine.
   * --------------------------------------------------------------------- */
  window.Histogram = {
    LEVELS,
    LEVEL_COLOR,
    DEFAULT_THRESHOLDS,
    bucketize,
    computeBins,
    quantilesExclusive,
    autoCalibrate,
    levelDistribution,
    genreDistribution,
  };
})();

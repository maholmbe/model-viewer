// Reads scene.json (written by the Blender add-on) and wires up
// model-viewer: home view, hotspots, close-up cameras and animations.

const viewer = document.querySelector('#viewer');
const info = document.querySelector('#info');
const infoTitle = document.querySelector('#info-title');
const infoText = document.querySelector('#info-text');
const backBtn = document.querySelector('#back');
const bgToggle = document.querySelector('#bg-toggle');
const variantBar = document.querySelector('#variants');

const vec = (v, unit = 'm') => v.map((c) => `${c}${unit}`).join(' ');
const orbit = ([theta, phi, radius]) => `${theta}deg ${phi}deg ${radius}m`;
const limit = ([theta, phi, radius]) =>
  [theta === 'auto' ? 'auto' : `${theta}deg`, `${phi}deg`, `${radius}m`].join(' ');

let scene;
let homeView;
let active = null;  // currently open hotspot
let token = 0;      // bumps on every navigation, cancels pending timers

async function init() {
  const res = await fetch('scene.json', { cache: 'no-cache' });
  scene = await res.json();
  document.title = scene.title;

  const env = scene.environment;
  const version = `?v=${scene.generatedAt}`;  // bust caches after a re-export
  viewer.src = scene.model.src + version;
  viewer.alt = scene.model.alt;
  // No cache-busting query on the HDR: model-viewer picks its loader from
  // the file extension and only treats URLs ending in ".hdr" as HDR.
  viewer.environmentImage = env.lighting;
  viewer.skyboxImage = env.skyboxVisible ? env.skybox : null;
  viewer.exposure = env.exposure;
  viewer.toneMapping = env.toneMapping;
  viewer.shadowIntensity = env.shadowIntensity;
  if (env.background) viewer.style.backgroundColor = env.background;

  const l = scene.limits;
  viewer.minCameraOrbit = limit(l.minOrbit);
  viewer.maxCameraOrbit = limit(l.maxOrbit);
  viewer.minFieldOfView = `${l.minFov}deg`;
  viewer.maxFieldOfView = `${l.maxFov}deg`;

  const cam = scene.home.camera;
  homeView = cam
    ? { orbit: orbit(cam.orbit), target: vec(cam.target), fov: `${cam.fov}deg` }
    : { orbit: '0deg 75deg 105%', target: 'auto auto auto', fov: 'auto' };
  setView(homeView);
  viewer.jumpCameraToGoal();

  scene.hotspots.forEach(addHotspot);

  if (env.backgroundToggle && env.skybox) {
    bgToggle.hidden = false;
    bgToggle.setAttribute('aria-pressed', String(env.skyboxVisible));
    bgToggle.addEventListener('click', () => {
      const show = !viewer.skyboxImage;
      viewer.skyboxImage = show ? env.skybox : null;
      bgToggle.setAttribute('aria-pressed', String(show));
    });
  }

  if (scene.variants?.picker) addVariantPicker(scene.variants);

  backBtn.addEventListener('click', () => leave());
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') leave(); });
  window.addEventListener('hashchange', openFromHash);

  viewer.addEventListener('load', () => {
    if (scene.home.animation) playClip(scene.home.animation.name, { mode: 'loop' });
    openFromHash();
    requestOcclusion();
  }, { once: true });
  viewer.addEventListener('camera-change', () => requestOcclusion());
  viewer.addEventListener('play', () => requestOcclusion());
  window.addEventListener('resize', () => requestOcclusion());
}

function addHotspot(h) {
  const btn = document.createElement('button');
  btn.className = 'mv-hotspot';
  btn.slot = `hotspot-${h.id.replace(/[^\w-]/g, '-')}`;
  btn.dataset.position = vec(h.position);
  btn.dataset.normal = vec(h.normal, '');
  // Pinned to a triangle of the mesh: follows the mesh when it animates.
  if (h.surface) btn.dataset.surface = h.surface;
  btn.setAttribute('aria-label', h.label);
  const label = document.createElement('span');
  label.className = 'mv-label';
  label.textContent = h.label;
  btn.append(label);
  btn.addEventListener('click', () => {
    if (active === h) return;
    location.hash = h.id;  // -> openFromHash -> enter(h)
  });
  h.element = btn;
  viewer.append(btn);
}

// --- Material variants -----------------------------------------------------
// One button per KHR_materials_variants variant. The model loads with the
// materials it had in Blender at export time; "current" names the variant
// that matches them, otherwise an "Original" button is added.

function addVariantPicker({ names, current }) {
  const buttons = [];
  const select = (name) => {
    viewer.variantName = name;
    for (const b of buttons) b.setAttribute('aria-pressed', String(b.dataset.variant === (name ?? '')));
  };
  const options = current ? names : ['', ...names];
  for (const name of options) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'mv-variant';
    b.dataset.variant = name;
    b.textContent = name || 'Original';
    b.setAttribute('aria-pressed', String(name === (current ?? '')));
    b.addEventListener('click', () => select(name || null));
    buttons.push(b);
    variantBar.append(b);
  }
  variantBar.hidden = false;
}

// --- Hotspot occlusion ---------------------------------------------------
// model-viewer only knows whether a hotspot's normal faces the camera. To hide
// hotspots that are behind other geometry, cast a ray from the camera through
// each hotspot and check whether the mesh is hit before the hotspot.
// Runs while the camera moves or an animation plays.

const OCCLUSION_INTERVAL = 100;  // ms between checks
let occlusionUntil = 0;
let occlusionTimer = null;

function requestOcclusion(ms = 300) {
  occlusionUntil = Math.max(occlusionUntil, performance.now() + ms);
  if (occlusionTimer == null) occlusionTick();
}

function occlusionTick() {
  updateOcclusion();
  if (performance.now() < occlusionUntil || !viewer.paused) {
    occlusionTimer = setTimeout(occlusionTick, OCCLUSION_INTERVAL);
  } else {
    occlusionTimer = null;
  }
}

function cameraPosition() {
  const { theta, phi, radius } = viewer.getCameraOrbit();
  const t = viewer.getCameraTarget();
  return {
    x: t.x + radius * Math.sin(phi) * Math.sin(theta),
    y: t.y + radius * Math.cos(phi),
    z: t.z + radius * Math.sin(phi) * Math.cos(theta),
  };
}

const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);

function updateOcclusion() {
  if (!viewer.loaded) return;
  const cam = cameraPosition();
  const rect = viewer.getBoundingClientRect();
  // Hotspots sit on the surface, so allow a little slack before the mesh
  // under the hotspot itself counts as "in front".
  const tolerance = scene.model.radius * 0.02;
  for (const h of scene.hotspots) {
    const q = viewer.queryHotspot(h.element.slot);
    let occluded = false;
    if (q) {
      const hit = viewer.positionAndNormalFromPoint(
        rect.x + q.canvasPosition.x, rect.y + q.canvasPosition.y);
      occluded = hit != null && distance(cam, hit.position) < distance(cam, q.position) - tolerance;
    }
    h.element.classList.toggle('mv-occluded', occluded);
  }
}

function openFromHash() {
  const id = decodeURIComponent(location.hash.slice(1));
  const h = scene.hotspots.find((x) => x.id === id);
  if (h) enter(h);
  else if (active) leave();
}

function setView({ orbit, target, fov }) {
  viewer.cameraTarget = target;
  viewer.cameraOrbit = orbit;
  viewer.fieldOfView = fov;
}

// Play an animation by name. Waits for model-viewer to apply the new
// animationName before calling play(), otherwise play-once options can be
// overwritten by the default infinite loop (model-viewer < 4.4).
async function playClip(name, { mode = 'once', reverse = false } = {}) {
  if (viewer.animationName !== name) {
    viewer.animationName = name;
    await viewer.updateComplete;
  }
  viewer.timeScale = reverse ? -1 : 1;
  if (mode === 'loop') viewer.play();
  else if (mode === 'pingpong') viewer.play({ pingpong: true });
  else viewer.play({ repetitions: 1 });
}

function after(seconds, fn) {
  const t = token;
  setTimeout(() => { if (t === token) fn(); }, seconds * 1000);
}

function enter(h) {
  if (active === h) return;
  token++;
  active?.element.classList.remove('mv-active');
  active = h;
  h.element.classList.add('mv-active');
  viewer.classList.add('mv-focused');

  infoTitle.textContent = h.label;
  infoText.textContent = h.description || '';
  info.hidden = false;

  if (h.camera) {
    setView({ orbit: orbit(h.camera.orbit), target: vec(h.camera.target), fov: `${h.camera.fov}deg` });
  }
  const anim = h.animation;
  if (anim) after(anim.delay, () => playClip(anim.name, { mode: anim.mode }));
}

function leave() {
  if (!active) return;
  token++;
  const h = active;
  active = null;
  h.element.classList.remove('mv-active');
  viewer.classList.remove('mv-focused');
  info.hidden = true;
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);

  setView(homeView);

  const anim = h.animation;
  const home = scene.home.animation;
  const playHome = () => { if (home) playClip(home.name, { mode: 'loop' }); };

  if (anim?.exit) {
    playClip(anim.exit).then(() => onFinished(playHome));
  } else if (anim?.reverseOnExit) {
    playClip(anim.name, { reverse: true }).then(() => onFinished(playHome));
  } else if (anim) {
    if (home) playHome();
    else { viewer.pause(); viewer.currentTime = 0; }
  } else {
    playHome();
  }
}

// Run fn when the current play-once animation finishes, unless the user
// has navigated somewhere else in the meantime.
function onFinished(fn) {
  const t = token;
  viewer.addEventListener('finished', () => {
    if (t !== token) return;
    viewer.timeScale = 1;
    fn();
  }, { once: true });
}

init().catch((err) => {
  console.error(err);
  document.body.insertAdjacentHTML('beforeend',
    '<p style="position:fixed;top:16px;left:16px;color:#b00020">Could not load scene.json – ' +
    'serve this folder over http (e.g. <code>python3 -m http.server</code>).</p>');
});

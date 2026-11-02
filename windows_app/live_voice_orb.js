(() => {
    const canvas = document.getElementById('live-voice-orb-canvas');
    const LOGICAL_SIZE = 1000;
    const ctx = canvas.getContext('2d');
    // The orb wrapper opens at the intro width (min(70vh, 620px, 88vw)) and then
    // animates down to --live-orb-size. The backing store used to be sized once,
    // from a formula based on the settled size, so the first frames were drawn
    // into fewer pixels than the intro box occupied and the orb read as soft
    // until the shrink transition caught up. Derive the backing store from the
    // real CSS box instead and re-sync whenever that box changes. Geometry is
    // untouched: every draw call stays in the 1000-unit logical space.
    const MIN_RENDER_SIZE = 600;
    const MAX_RENDER_SIZE = 2560;
    const RENDER_STEP = 32;
    let renderScale = 1;
    let pendingResolutionSync = true;

    function measuredOrbCssSize() {
      const rect = canvas.getBoundingClientRect();
      const measured = Math.max(rect.width, rect.height);
      if (measured > 1) return measured;
      // Overlay still collapsed: fall back to the widest box the intro can use.
      return Math.min(window.innerHeight * 0.7, 620, window.innerWidth * 0.88);
    }

    function syncRenderResolution() {
      const pixelRatio = window.devicePixelRatio || 1;
      const desired = measuredOrbCssSize() * pixelRatio;
      const target = Math.min(
        MAX_RENDER_SIZE,
        Math.max(MIN_RENDER_SIZE, Math.ceil(desired / RENDER_STEP) * RENDER_STEP)
      );
      if (canvas.width === target && canvas.height === target) return;
      // Assigning width/height clears the surface and resets the transform.
      canvas.width = target;
      canvas.height = target;
      renderScale = target / LOGICAL_SIZE;
      ctx.setTransform(renderScale, 0, 0, renderScale, 0, 0);
    }

    syncRenderResolution();
    pendingResolutionSync = true;
    if (typeof ResizeObserver === 'function') {
      new ResizeObserver(() => { pendingResolutionSync = true; }).observe(canvas);
    }
    window.addEventListener('resize', () => { pendingResolutionSync = true; });
    window.addEventListener('orientationchange', () => { pendingResolutionSync = true; });
    const cx = LOGICAL_SIZE / 2;
    const cy = LOGICAL_SIZE / 2;

    const NUM_NODES = 350;
    const EXTRA_COUNT = 350;
    const TOTAL_STRUCT_NODES = NUM_NODES + EXTRA_COUNT;
    const RADIUS = 320;
    const CONNECTION_DIST = 95;

    let nodes = [];
    let shieldExtraNodes = [];

    function lerp(a, b, t) {
      return a + (b - a) * t;
    }

    function clamp(v, min, max) {
      return Math.max(min, Math.min(max, v));
    }

    function mapVal(val, min1, max1, min2, max2) {
      return min2 + (val - min1) * (max2 - min2) / (max1 - min1);
    }

    // ------------------------------------------------------------
    // BASE ORB
    // Idle + Listening keep the original random volumetric network.
    // ------------------------------------------------------------
    function initNodes() {
      for (let i = 0; i < NUM_NODES; i++) {
        const u = Math.random();
        const v = Math.random();
        const theta = u * 2.0 * Math.PI;
        const phi = Math.acos(2.0 * v - 1.0);
        const r = Math.pow(Math.random(), 0.6) * RADIUS;

        const baseX = r * Math.sin(phi) * Math.cos(theta);
        const baseY = r * Math.sin(phi) * Math.sin(theta);
        const baseZ = r * Math.cos(phi);

        nodes.push({
          idx: i,
          theta,
          phi,
          baseR: r,

          baseX,
          baseY,
          baseZ,

          x: baseX,
          y: baseY,
          z: baseZ,

          hx: 0,
          hy: 0,
          hz: 0,

          activation: 0,
          neighbors: [],
          alpha: 1,
          isExtra: false
        });
      }

      // Original base connections.
      for (let i = 0; i < NUM_NODES; i++) {
        const n1 = nodes[i];

        for (let j = i + 1; j < NUM_NODES; j++) {
          const n2 = nodes[j];
          const dx = n1.baseX - n2.baseX;
          const dy = n1.baseY - n2.baseY;
          const dz = n1.baseZ - n2.baseZ;

          if (Math.sqrt(dx * dx + dy * dy + dz * dz) < CONNECTION_DIST) {
            n1.neighbors.push(j);
            n2.neighbors.push(i);
          }
        }
      }

      // ----------------------------------------------------------
      // THINKING SHIELD
      // This is the shielding idea from the original file.
      // ----------------------------------------------------------
      for (let i = 0; i < NUM_NODES; i++) {
        const n = nodes[i];

        let targetR;
        let newTheta;
        let newPhi;

        const isInner = n.baseR < RADIUS * 0.60;

        if (isInner) {
          // Inner shell is now more symmetric and round.
          targetR =
            RADIUS * 0.505 +
            Math.sin(n.phi * 3.0) * 2.5;

          newTheta = n.theta;
          newPhi = n.phi;

        } else {
          // Rounded shield: still keeps the original six-sided identity,
          // but pushes it much closer to a sphere so it reads as a clean orb.
          const roundedHexRadius = RADIUS * 0.89;

          const segment =
            Math.floor(n.theta / (Math.PI / 3));

          const segmentCenter =
            segment * (Math.PI / 3) +
            Math.PI / 6;

          let localTheta =
            n.theta - segmentCenter;

          // Mirror every segment around its own center for symmetry.
          localTheta = clamp(
            localTheta,
            -Math.PI / 6,
            Math.PI / 6
          );

          newTheta =
            segmentCenter + localTheta;

          // Gentle hex modulation instead of hard faceting.
          const hexMod =
            1.0 +
            0.055 *
            Math.cos(localTheta * 6.0);

          newPhi = mapVal(
            n.phi,
            0,
            Math.PI,
            0.20,
            Math.PI - 0.20
          );

          // Near-spherical silhouette with only subtle shield character.
          const verticalRound =
            0.93 +
            0.07 * Math.sin(newPhi);

          targetR =
            roundedHexRadius *
            hexMod *
            verticalRound;
        }

        n.hx =
          targetR *
          Math.sin(newPhi) *
          Math.cos(newTheta);

        n.hy =
          targetR *
          Math.sin(newPhi) *
          Math.sin(newTheta);

        n.hz =
          targetR *
          Math.cos(newPhi);
      }

      // Extra nodes exist only to enrich structured states.
      // They start hidden at a parent node so they never pop in from nowhere.
      for (let i = 0; i < EXTRA_COUNT; i++) {
        const parent = nodes[i % NUM_NODES];

        const u = Math.random();
        const v = Math.random();
        const theta = u * 2.0 * Math.PI;
        const phi = Math.acos(2.0 * v - 1.0);
        // Bias most extra Thinking dots toward the outer shield so its
        // external surface becomes visibly denser without affecting Idle.
        const outerBiased =
          i < Math.floor(EXTRA_COUNT * 0.78);

        const baseR =
          outerBiased
            ? RADIUS * (0.64 + Math.random() * 0.34)
            : Math.pow(Math.random(), 0.6) * RADIUS;

        let targetR;
        let newTheta;
        let newPhi;

        const isInner = baseR < RADIUS * 0.60;

        if (isInner) {
          targetR =
            RADIUS * 0.505 +
            Math.sin(phi * 3.0) * 2.5;

          newTheta = theta;
          newPhi = phi;

        } else {
          const roundedHexRadius = RADIUS * 0.89;

          const segment =
            Math.floor(theta / (Math.PI / 3));

          const segmentCenter =
            segment * (Math.PI / 3) +
            Math.PI / 6;

          let localTheta =
            theta - segmentCenter;

          localTheta = clamp(
            localTheta,
            -Math.PI / 6,
            Math.PI / 6
          );

          newTheta =
            segmentCenter + localTheta;

          const hexMod =
            1.0 +
            0.055 *
            Math.cos(localTheta * 6.0);

          newPhi = mapVal(
            phi,
            0,
            Math.PI,
            0.20,
            Math.PI - 0.20
          );

          const verticalRound =
            0.93 +
            0.07 * Math.sin(newPhi);

          targetR =
            roundedHexRadius *
            hexMod *
            verticalRound;
        }

        shieldExtraNodes.push({
          idx: NUM_NODES + i,
          isExtra: true,
          parent,

          baseX: parent.baseX,
          baseY: parent.baseY,
          baseZ: parent.baseZ,

          x: parent.baseX,
          y: parent.baseY,
          z: parent.baseZ,

          hx:
            targetR *
            Math.sin(newPhi) *
            Math.cos(newTheta),

          hy:
            targetR *
            Math.sin(newPhi) *
            Math.sin(newTheta),

          hz:
            targetR *
            Math.cos(newPhi),

          alpha: 0,
          activation: 0
        });
      }
    }


    initNodes();

    const structureNodes = [...nodes, ...shieldExtraNodes];

    // ------------------------------------------------------------
    // GUARANTEED BASE TOPOLOGY
    // Idle + Listening used to rely only on distance-based neighbors,
    // which could leave isolated dots. Add nearest-neighbor fallback
    // edges so every visible base dot belongs to the network.
    // ------------------------------------------------------------
    const baseEdges = [];

    function buildBaseEdges() {
      const edgeSet = new Set();

      function addEdge(a, b, strength = 1.0) {
        if (a === b) return;
        const lo = Math.min(a, b);
        const hi = Math.max(a, b);
        const key = `${lo}:${hi}`;

        if (edgeSet.has(key)) return;
        edgeSet.add(key);
        baseEdges.push({ a: lo, b: hi, strength });
      }

      // Keep all original neighbor edges.
      for (let i = 0; i < NUM_NODES; i++) {
        for (const j of nodes[i].neighbors) {
          if (j > i) addEdge(i, j, 1.0);
        }
      }

      // Ensure every node has at least 2 connections.
      const degree = new Array(NUM_NODES).fill(0);
      for (const e of baseEdges) {
        degree[e.a]++;
        degree[e.b]++;
      }

      for (let i = 0; i < NUM_NODES; i++) {
        if (degree[i] >= 2) continue;

        const nearest = [];

        for (let j = 0; j < NUM_NODES; j++) {
          if (i === j) continue;

          const dx = nodes[i].baseX - nodes[j].baseX;
          const dy = nodes[i].baseY - nodes[j].baseY;
          const dz = nodes[i].baseZ - nodes[j].baseZ;
          const d2 = dx * dx + dy * dy + dz * dz;

          nearest.push({ j, d2 });
        }

        nearest.sort((a, b) => a.d2 - b.d2);

        for (const item of nearest) {
          if (degree[i] >= 2) break;

          const before = baseEdges.length;
          addEdge(i, item.j, 0.55);

          if (baseEdges.length > before) {
            degree[i]++;
            degree[item.j]++;
          }
        }
      }
    }

    buildBaseEdges();

    // ------------------------------------------------------------
    // GUARANTEED SHIELD TOPOLOGY
    // Every visible shield dot receives multiple lines.
    // This fixes the orphan-dot problem.
    // ------------------------------------------------------------
    const shieldEdges = [];

    function buildNearestNeighborEdges(targetGetter, neighborCount = 3) {
      const edgeSet = new Set();
      const result = [];

      for (let i = 0; i < structureNodes.length; i++) {
        const a = targetGetter(structureNodes[i], i);

        // Outer Thinking surface intentionally carries more lines.
        const aRadius =
          Math.sqrt(
            a.x * a.x +
            a.y * a.y +
            a.z * a.z
          );

        const desiredNeighbors =
          aRadius > RADIUS * 0.72
            ? 5
            : neighborCount;

        const nearest = [];

        for (let j = 0; j < structureNodes.length; j++) {
          if (i === j) continue;

          const b = targetGetter(structureNodes[j], j);

          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const dz = a.z - b.z;

          const d2 = dx * dx + dy * dy + dz * dz;

          let insertAt = nearest.length;

          for (let k = 0; k < nearest.length; k++) {
            if (d2 < nearest[k].d2) {
              insertAt = k;
              break;
            }
          }

          nearest.splice(insertAt, 0, { j, d2 });

          if (nearest.length > desiredNeighbors) {
            nearest.pop();
          }
        }

        for (const item of nearest) {
          const aIdx = Math.min(i, item.j);
          const bIdx = Math.max(i, item.j);
          const key = `${aIdx}:${bIdx}`;

          if (!edgeSet.has(key)) {
            edgeSet.add(key);

            result.push({
              a: aIdx,
              b: bIdx,
              strength: 1.0
            });
          }
        }
      }

      return result;
    }

    shieldEdges.push(
      ...buildNearestNeighborEdges(
        n => ({ x: n.hx, y: n.hy, z: n.hz }),
        3
      )
    );

    // ------------------------------------------------------------
    // SPEAKING STRUCTURE: VOICE CAPSULE
    // Completely different from the Thinking shield.
    //
    // 20 horizontal rings x 35 points = 700 structured nodes.
    // Rings ripple according to speech energy.
    // Every point is connected to its ring neighbors and vertical rails.
    // ------------------------------------------------------------
    const SPEAK_RINGS = 20;
    const SPEAK_POINTS_PER_RING = 35;
    const speakingEdges = [];

    function getSpeakingBaseTarget(index) {
      const ring =
        Math.floor(index / SPEAK_POINTS_PER_RING);

      const point =
        index % SPEAK_POINTS_PER_RING;

      const yNorm =
        mapVal(
          ring,
          0,
          SPEAK_RINGS - 1,
          -1,
          1
        );

      const envelope =
        Math.pow(
          Math.max(0, 1 - yNorm * yNorm),
          0.62
        );

      // This is intentionally the "mid-transition" geometry:
      // part rounded shield / part audio capsule.
      const capsuleRadius =
        72 + 185 * envelope;

      const angle =
        (point / SPEAK_POINTS_PER_RING) *
        Math.PI * 2;

      const capsule = {
        x: capsuleRadius * Math.cos(angle),
        y: yNorm * 255,
        z: capsuleRadius * Math.sin(angle)
      };

      const shieldNode =
        structureNodes[index];

      const shield = {
        x: shieldNode.hx,
        y: shieldNode.hy,
        z: shieldNode.hz
      };

      // Midpoint biased slightly toward the shield because that transition
      // frame was the visually stronger one.
      const mix = 0.90;

      return {
        x: lerp(shield.x, capsule.x, mix),
        y: lerp(shield.y, capsule.y, mix),
        z: lerp(shield.z, capsule.z, mix)
      };
    }

    function buildSpeakingEdges() {
      const edgeSet = new Set();

      function addEdge(a, b, strength) {
        if (a === b) return;

        const lo = Math.min(a, b);
        const hi = Math.max(a, b);
        const key = `${lo}:${hi}`;

        if (edgeSet.has(key)) return;

        edgeSet.add(key);
        speakingEdges.push({ a: lo, b: hi, strength });
      }

      for (let ring = 0; ring < SPEAK_RINGS; ring++) {
        for (let point = 0; point < SPEAK_POINTS_PER_RING; point++) {
          const idx =
            ring * SPEAK_POINTS_PER_RING + point;

          const nextPoint =
            ring * SPEAK_POINTS_PER_RING +
            ((point + 1) % SPEAK_POINTS_PER_RING);

          const prevPoint =
            ring * SPEAK_POINTS_PER_RING +
            ((point - 1 + SPEAK_POINTS_PER_RING) %
              SPEAK_POINTS_PER_RING);

          addEdge(idx, nextPoint, 1.0);
          addEdge(idx, prevPoint, 1.0);

          // Vertical rails.
          if (ring < SPEAK_RINGS - 1) {
            addEdge(
              idx,
              (ring + 1) * SPEAK_POINTS_PER_RING + point,
              0.34
            );
          }

          // Sparse diagonals make it feel like one 3D audio lattice.
          if (
            ring < SPEAK_RINGS - 1 &&
            point % 3 === 0
          ) {
            addEdge(
              idx,
              (ring + 1) * SPEAK_POINTS_PER_RING +
                ((point + 1) % SPEAK_POINTS_PER_RING),
              0.16
            );
          }
        }
      }
    }

    buildSpeakingEdges();

    // ------------------------------------------------------------
    // UI / STATE -- driven by Jupiter's live-voice controller.
    let orbState = 'idle';
    let semanticOrbState = 'idle';
    let isSpeaking = false;
    let wordPulse = 0;
    let targetWordPulse = 0;
    let audioSpeechEnergy = 0;
    let audioEnergyUpdatedAt = 0;
    let listeningInputEnergy = 0;
    let listeningEnergyUpdatedAt = 0;
    let time = 0;
    let returnStartedAt = 0;
    let returnProgress = 0;
    const RETURN_DURATION = 1.7;
    let currentGlobalScale = 0.9;
    let isActive = false;
    let animationFrameId = null;
    let lastFrameTimestamp = 0;
    let renderingSuspended = document.visibilityState === 'hidden';
    const MAX_FRAME_RATE = 60;
    const MIN_FRAME_INTERVAL = (1000 / MAX_FRAME_RATE) * 0.92;

    const THEMES = {
      idle: { r: 50, g: 180, b: 255 },
      listening: { r: 50, g: 180, b: 255 },
      thinking: { r: 0, g: 150, b: 255 },
      speaking: { r: 0, g: 180, b: 255 }
    };

    let currentColor = { r: 50, g: 180, b: 255 };
    // ── Spectrum mode (Camera Live floating orb) ─────────────────────────────────────────────────────────────────
    // The RGB lives INSIDE the orb geometry (lines, nodes, core), never as an external halo: a premium palette flows
    // around the sphere by angle, at a state-dependent tempo (waiting/idle very slow, listening calm, thinking fast,
    // speaking follows audio energy). Saturated mid-lightness colours plus a thin dark rim on nodes keep the orb
    // recognisable on white walls/sky/shirts. Smooth interpolation only (no flashing); reduced motion slows it.
    const SPECTRUM_STOPS = [
      [0.00, [37, 99, 235]],    // blue      (deep, saturated stops stay readable on white walls/sky/shirts)
      [0.20, [6, 160, 200]],    // cyan
      [0.38, [5, 150, 105]],    // green
      [0.46, [217, 119, 6]],    // small warm accent
      [0.56, [219, 39, 119]],   // magenta
      [0.78, [124, 58, 237]],   // violet
      [1.00, [37, 99, 235]]     // back to blue
    ];
    // Spectrum is a BLEND (0 = speech-only blue, 1 = camera RGB) so the camera transition can cross-fade the colours
    // inside the same geometry instead of switching abruptly.
    let spectrumMix = 0;
    let spectrumTarget = 0;
    let spectrumBlendMs = 0;
    let spectrumMixAt = 0;
    let spectrumPhase = 0;
    let spectrumAt = 0;
    const updateSpectrumMix = (now) => {
      if (spectrumMix === spectrumTarget) { spectrumMixAt = now; return; }
      const dt = spectrumMixAt ? Math.max(0, now - spectrumMixAt) : 0;
      spectrumMixAt = now;
      const step = spectrumBlendMs > 0 ? dt / spectrumBlendMs : 1;
      spectrumMix = spectrumTarget > spectrumMix ? Math.min(spectrumTarget, spectrumMix + step)
        : Math.max(spectrumTarget, spectrumMix - step);
    };
    const blendRgba = (base, spectral, baseAlpha, spectralAlpha) => {
      const k = spectrumMix;
      const channel = (index) => Math.round(base[index] + (spectral[index] - base[index]) * k);
      return `rgba(${channel(0)}, ${channel(1)}, ${channel(2)}, ${Math.min(1, baseAlpha + (spectralAlpha - baseAlpha) * k)})`;
    };
    const spectrumReducedMotion = () => Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches);
    const spectrumColorAt = (t, lift = 0) => {
      const u = ((t % 1) + 1) % 1;
      let index = 1;
      while (index < SPECTRUM_STOPS.length - 1 && SPECTRUM_STOPS[index][0] < u) index += 1;
      const [p0, c0] = SPECTRUM_STOPS[index - 1];
      const [p1, c1] = SPECTRUM_STOPS[index];
      const k = (u - p0) / Math.max(0.0001, p1 - p0);
      const mix = (a, b) => a + (b - a) * (k * k * (3 - 2 * k));            // smoothstep between stops
      const bright = Math.min(0.35, Math.max(0, lift) * 0.35);
      return c0.map((value, channel) => Math.round(mix(value, c1[channel]) + (255 - mix(value, c1[channel])) * bright));
    };
    const spectrumRgb = (x, y, lift = 0) => {
      const angle = Math.atan2(y - LOGICAL_SIZE / 2, x - LOGICAL_SIZE / 2) / (Math.PI * 2);
      return spectrumColorAt(angle + spectrumPhase, lift);
    };
    const advanceSpectrum = (now, energy = 0) => {
      const dt = spectrumAt ? Math.min(0.1, Math.max(0, (now - spectrumAt) / 1000)) : 0;
      spectrumAt = now;
      const tempo = orbState === 'thinking' ? 0.16
        : orbState === 'speaking' ? 0.07 + Math.min(1, energy) * 0.12
          : orbState === 'listening' ? 0.05
            : 0.018;                                                       // idle / WAITING FOR YOU: very slow
      spectrumPhase = (spectrumPhase + dt * tempo * (spectrumReducedMotion() ? 0.25 : 1)) % 1;
    };

    function setOrbState(state) {
      if (!['idle', 'listening', 'thinking', 'preparing', 'speaking', 'returning'].includes(state)) return false;
      semanticOrbState = state === 'returning' ? 'idle' : state;
      const visualState = state === 'preparing' ? 'thinking' : state;
      const nextState = visualState === 'idle' && orbState === 'speaking' ? 'returning' : visualState;
      orbState = nextState;
      isSpeaking = nextState === 'speaking';
      if (nextState === 'returning') {
        returnStartedAt = time;
        returnProgress = 0;
      }
      return true;
    }

    // DRAG ROTATION
    // ------------------------------------------------------------
    let baseRotY = 0;
    let baseRotX = 0;
    let userRotY = 0;
    let userRotX = 0;
    let userVelY = 0;
    let userVelX = 0;

    let isDragging = false;
    let previousPointerX = 0;
    let previousPointerY = 0;

    canvas.style.touchAction = 'none';

    canvas.addEventListener(
      'pointerdown',
      e => {
        isDragging = true;

        previousPointerX = e.clientX;
        previousPointerY = e.clientY;

        userVelX = 0;
        userVelY = 0;

        canvas.setPointerCapture(e.pointerId);
        canvas.style.cursor = 'grabbing';
      }
    );

    canvas.addEventListener(
      'pointermove',
      e => {
        if (!isDragging) return;

        const deltaX =
          e.clientX - previousPointerX;

        const deltaY =
          e.clientY - previousPointerY;

        const rY = deltaX * 0.008;
        const rX = deltaY * 0.008;

        userRotY += rY;
        userRotX += rX;

        userVelY = rY;
        userVelX = rX;

        previousPointerX = e.clientX;
        previousPointerY = e.clientY;
      }
    );

    canvas.addEventListener(
      'pointerup',
      e => {
        isDragging = false;
        canvas.releasePointerCapture(e.pointerId);
        canvas.style.cursor = 'grab';
      }
    );

    canvas.addEventListener(
      'pointercancel',
      e => {
        isDragging = false;
        canvas.releasePointerCapture(e.pointerId);
        canvas.style.cursor = 'grab';
      }
    );

    canvas.addEventListener(
      'pointerenter',
      () => {
        if (!isDragging) {
          canvas.style.cursor = 'grab';
        }
      }
    );

    // ------------------------------------------------------------
    // RENDER
    // ------------------------------------------------------------
    function draw(frameTimestamp = performance.now()) {
      if (!isActive || renderingSuspended) {
        animationFrameId = null;
        return;
      }
      animationFrameId = requestAnimationFrame(draw);
      if (lastFrameTimestamp && frameTimestamp - lastFrameTimestamp < MIN_FRAME_INTERVAL) return;

      if (pendingResolutionSync) {
        pendingResolutionSync = false;
        syncRenderResolution();
      }

      const elapsedSeconds = lastFrameTimestamp
        ? Math.min((frameTimestamp - lastFrameTimestamp) / 1000, 0.05)
        : 0.016;
      const frameStep = elapsedSeconds / 0.016;
      lastFrameTimestamp = frameTimestamp;
      time += elapsedSeconds;

      const audioEnergyIsFresh =
        performance.now() - audioEnergyUpdatedAt < 140;
      const desiredWordPulse =
        isSpeaking
          ? (audioEnergyIsFresh ? Math.max(0.035, audioSpeechEnergy) : 0.035)
          : 0;
      targetWordPulse = lerp(
        targetWordPulse,
        desiredWordPulse,
        desiredWordPulse > targetWordPulse ? 0.34 : 0.12
      );
      wordPulse = lerp(wordPulse, targetWordPulse, 0.18);

      if (orbState === 'returning') {
        returnProgress =
          clamp(
            (time - returnStartedAt) /
              RETURN_DURATION,
            0,
            1
          );
      }

      // Smoothstep removes the visible hitch near the final return frame.
      const returnEase =
        orbState === 'returning'
          ? (
              returnProgress *
              returnProgress *
              (3 - 2 * returnProgress)
            )
          : 0;

      let globalScaleTarget = 0.9;
      let rotationSpeed = 0.002;
      let coreGlow = 0.2;

      if (orbState === 'listening') {
        // Listening now subtly squeezes small -> big in sync with the
        // inward travelling light wave.
        const listenCycle =
          ((time * 100) % RADIUS) / RADIUS;

        const squeezePulse =
          -Math.cos(listenCycle * Math.PI * 2) * 0.012;

        globalScaleTarget =
          0.9 + squeezePulse + Math.sin(time * 2) * 0.006 +
          (performance.now() - listeningEnergyUpdatedAt < 180 ? listeningInputEnergy * 0.035 : 0);

        const liveInputEnergy = performance.now() - listeningEnergyUpdatedAt < 180 ? listeningInputEnergy : 0;
        rotationSpeed = 0.004 + liveInputEnergy * 0.002;
        coreGlow = 0.34 + liveInputEnergy * 0.18;

      } else if (orbState === 'thinking') {
        // Shield concept from the original version.
        globalScaleTarget = 0.69;
        rotationSpeed = 0.0062;
        coreGlow = 0.31;

      } else if (orbState === 'speaking') {
        globalScaleTarget =
          0.76 + wordPulse * 0.025;

        rotationSpeed =
          0.0027 + wordPulse * 0.002;

        coreGlow =
          0.25 + wordPulse * 0.28;

      } else if (orbState === 'returning') {
        // Continuously blend the Speaking motion envelope into Idle.
        globalScaleTarget =
          lerp(
            0.76,
            0.9,
            returnEase
          );

        rotationSpeed =
          lerp(
            0.0027,
            0.002,
            returnEase
          );

        coreGlow =
          lerp(
            0.25,
            0.2,
            returnEase
          );

      } else {
        // Idle effect/color unchanged; only footprint = Listening.
        globalScaleTarget = 0.9;
        rotationSpeed = 0.002;
        coreGlow = 0.2;
      }

      currentGlobalScale =
        lerp(
          currentGlobalScale,
          globalScaleTarget,
          0.05
        );

      const targetColor =
        orbState === 'returning'
          ? THEMES.idle
          : THEMES[orbState];

      currentColor.r =
        lerp(
          currentColor.r,
          targetColor.r,
          0.05
        );

      currentColor.g =
        lerp(
          currentColor.g,
          targetColor.g,
          0.05
        );

      currentColor.b =
        lerp(
          currentColor.b,
          targetColor.b,
          0.05
        );

      if (!isDragging) {
        userVelY *= Math.pow(0.94, frameStep);
        userVelX *= Math.pow(0.94, frameStep);

        userRotY += userVelY;
        userRotX += userVelX;
      }

      baseRotY += rotationSpeed * frameStep;
      baseRotX += rotationSpeed * 0.6 * frameStep;

      const rotY =
        baseRotY + userRotY;

      const rotX =
        baseRotX + userRotX;

      ctx.clearRect(
        0,
        0,
        LOGICAL_SIZE,
        LOGICAL_SIZE
      );

      const radGrad =
        ctx.createRadialGradient(
          cx,
          cy,
          0,
          cx,
          cy,
          RADIUS *
            currentGlobalScale *
            1.5
        );

      updateSpectrumMix(performance.now());
      if (spectrumMix > 0) advanceSpectrum(performance.now(), (typeof audioSpeechEnergy === 'number' ? audioSpeechEnergy : 0));
      const spectralCore = spectrumMix > 0 ? spectrumColorAt(spectrumPhase + 0.25) : null;
      const coreColor = spectralCore
        ? [0, 1, 2].map((i) => Math.round([currentColor.r, currentColor.g, currentColor.b][i]
          + (spectralCore[i] - [currentColor.r, currentColor.g, currentColor.b][i]) * spectrumMix))
        : [currentColor.r, currentColor.g, currentColor.b];
      radGrad.addColorStop(
        0,
        `rgba(
          ${coreColor[0]},
          ${coreColor[1]},
          ${coreColor[2]},
          ${coreGlow * (1 - 0.45 * spectrumMix)}
        )`
      );

      radGrad.addColorStop(
        0.4,
        `rgba(
          ${coreColor[0]},
          ${coreColor[1]},
          ${coreColor[2]},
          ${coreGlow * 0.2}
        )`
      );

      radGrad.addColorStop(
        1,
        'rgba(0,0,0,0)'
      );

      ctx.globalCompositeOperation =
        'screen';

      ctx.fillStyle = radGrad;

      ctx.fillRect(
        0,
        0,
        LOGICAL_SIZE,
        LOGICAL_SIZE
      );
      // Spectrum geometry is drawn with normal compositing so saturated colours stay saturated on bright scenes.
      ctx.globalCompositeOperation = spectrumMix >= 0.5 ? 'source-over' : 'screen';

      // Listening: reinforce the inner orb so the inside stays visible
      // while keeping the main color blue.
      if (orbState === 'listening') {
        const innerGrad =
          ctx.createRadialGradient(
            cx,
            cy,
            0,
            cx,
            cy,
            RADIUS *
              currentGlobalScale *
              0.60
          );

        innerGrad.addColorStop(
          0,
          'rgba(255,255,255,0.15)'
        );

        innerGrad.addColorStop(
          0.22,
          `rgba(
            ${Math.min(255, currentColor.r + 35)},
            ${Math.min(255, currentColor.g + 35)},
            ${Math.min(255, currentColor.b + 45)},
            0.16
          )`
        );

        innerGrad.addColorStop(
          0.50,
          `rgba(
            ${currentColor.r},
            ${currentColor.g},
            ${currentColor.b},
            0.07
          )`
        );

        innerGrad.addColorStop(
          1,
          'rgba(0,0,0,0)'
        );

        ctx.fillStyle = innerGrad;
        ctx.fillRect(
          0,
          0,
          LOGICAL_SIZE,
          LOGICAL_SIZE
        );
      }

      // Extra inner-core emphasis for Thinking so the inner orb remains
      // visibly present beneath the animated outer shell.
      if (orbState === 'thinking') {
        const innerGrad =
          ctx.createRadialGradient(
            cx,
            cy,
            0,
            cx,
            cy,
            RADIUS *
              currentGlobalScale *
              0.58
          );

        innerGrad.addColorStop(
          0,
          `rgba(
            ${Math.min(255, currentColor.r + 30)},
            ${Math.min(255, currentColor.g + 45)},
            ${Math.min(255, currentColor.b + 55)},
            0.24
          )`
        );

        innerGrad.addColorStop(
          0.35,
          `rgba(
            ${Math.min(255, currentColor.r + 20)},
            ${Math.min(255, currentColor.g + 35)},
            ${Math.min(255, currentColor.b + 45)},
            0.14
          )`
        );

        innerGrad.addColorStop(
          0.7,
          `rgba(
            ${currentColor.r},
            ${currentColor.g},
            ${currentColor.b},
            0.03
          )`
        );

        innerGrad.addColorStop(
          1,
          'rgba(0,0,0,0)'
        );

        ctx.fillStyle = innerGrad;
        ctx.fillRect(
          0,
          0,
          LOGICAL_SIZE,
          LOGICAL_SIZE
        );
      }

      const cosY = Math.cos(rotY);
      const sinY = Math.sin(rotY);
      const cosX = Math.cos(rotX);
      const sinX = Math.sin(rotX);
      const fov = 700;

      const projMap =
        new Array(TOTAL_STRUCT_NODES);

      // ----------------------------------------------------------
      // POSITION + ACTIVATION
      // ----------------------------------------------------------
      for (
        let i = 0;
        i < TOTAL_STRUCT_NODES;
        i++
      ) {
        const n = structureNodes[i];

        let targetX;
        let targetY;
        let targetZ;

        let targetAlpha =
          n.isExtra ? 0 : 1;

        if (
          orbState === 'idle' ||
          orbState === 'listening'
        ) {
          if (n.isExtra) {
            targetX = n.parent.baseX;
            targetY = n.parent.baseY;
            targetZ = n.parent.baseZ;
            targetAlpha = 0;
          } else {
            targetX = n.baseX;
            targetY = n.baseY;
            targetZ = n.baseZ;
            targetAlpha = 1;
          }

        } else if (
          orbState === 'thinking'
        ) {
          const shieldR =
            Math.sqrt(
              n.hx * n.hx +
              n.hy * n.hy +
              n.hz * n.hz
            ) || 1;

          const isOuterShield =
            shieldR > RADIUS * 0.72;

          if (isOuterShield) {
            // Symmetric wave bands travel around the OUTER shell.
            const shellPhi =
              Math.acos(
                clamp(
                  n.hz / shieldR,
                  -1,
                  1
                )
              );

            const wave =
              Math.sin(
                shellPhi * 6.0 -
                time * 5.0
              );

            const waveOffset =
              wave * 7.5;

            const waveScale =
              (shieldR + waveOffset) /
              shieldR;

            targetX =
              n.hx * waveScale;

            targetY =
              n.hy * waveScale;

            targetZ =
              n.hz * waveScale;

          } else {
            targetX = n.hx;
            targetY = n.hy;
            targetZ = n.hz;
          }

          targetAlpha = 1;

        } else if (
          orbState === 'speaking'
        ) {
          const base =
            getSpeakingBaseTarget(i);

          const ring =
            Math.floor(
              i / SPEAK_POINTS_PER_RING
            );

          const point =
            i % SPEAK_POINTS_PER_RING;

          const phase =
            (point /
              SPEAK_POINTS_PER_RING) *
            Math.PI *
            2;

          const yNorm =
            mapVal(
              ring,
              0,
              SPEAK_RINGS - 1,
              -1,
              1
            );

          const fallback =
            isSpeaking
              ? 0.10 +
                0.04 *
                  Math.pow(
                    Math.sin(time * 5.5),
                    2
                  )
              : 0;

          const energy =
            Math.max(
              wordPulse,
              fallback
            );

          // Wave travels vertically through the capsule.
          const bandWave =
            Math.sin(
              yNorm *
                Math.PI *
                5.0 -
              time * 10.0
            );

          // Secondary harmonic travels around each ring.
          const ringWave =
            Math.sin(
              phase * 3.0 +
              time * 7.0 +
              ring * 0.28
            );

          const deformation =
            1 +
            energy *
              (
                bandWave * 0.12 +
                ringWave * 0.035
              );

          targetX =
            base.x * deformation;

          targetY =
            base.y *
            (
              1 +
              energy *
                bandWave *
                0.018
            );

          targetZ =
            base.z * deformation;

          targetAlpha = 1;

        } else {
          // RETURNING: keep the current Speaking node positions alive and
          // continuously pull them into Idle. Extra structured dots dissolve
          // while the permanent Idle dots remain visible.
          if (n.isExtra) {
            targetX = n.parent.baseX;
            targetY = n.parent.baseY;
            targetZ = n.parent.baseZ;

            targetAlpha =
              1 - returnEase;
          } else {
            targetX = n.baseX;
            targetY = n.baseY;
            targetZ = n.baseZ;
            targetAlpha = 1;
          }
        }

        const morphRate =
          (
            orbState === 'thinking' ||
            orbState === 'speaking'
          )
            ? 0.05
            : orbState === 'returning'
              ? 0.06
              : 0.025;

        n.x =
          lerp(
            n.x,
            targetX,
            morphRate
          );

        n.y =
          lerp(
            n.y,
            targetY,
            morphRate
          );

        n.z =
          lerp(
            n.z,
            targetZ,
            morphRate
          );

        n.alpha =
          lerp(
            n.alpha,
            targetAlpha,
            n.isExtra ? 0.08 : 0.12
          );

        // No wandering dots outside structured states.
        const driftAmount =
          (
            orbState === 'thinking' ||
            orbState === 'speaking'
          )
            ? 0
            : orbState === 'returning'
              ? 8 * returnEase
              : 8;

        const driftX =
          Math.sin(time * 0.5 + i) *
          driftAmount;

        const driftY =
          Math.cos(time * 0.4 + i) *
          driftAmount;

        const driftZ =
          Math.sin(
            time * 0.6 + i * 2
          ) *
          driftAmount;

        const distToCenter =
          Math.sqrt(
            n.x * n.x +
            n.y * n.y +
            n.z * n.z
          );

        if (
          orbState === 'listening' &&
          !n.isExtra
        ) {
          const inwardWave =
            RADIUS -
            ((time * 100) % RADIUS);

          // Keep a clearer visible interior even between wave passes.
          const centerBias =
            Math.max(
              0,
              1 - distToCenter / (RADIUS * 0.92)
            );

          n.activation =
            lerp(
              n.activation,
              0.06 + centerBias * 0.20,
              0.12
            );

          // White wave moving from outside to inside.
          if (
            Math.abs(
              distToCenter -
              inwardWave
            ) < 30
          ) {
            n.activation =
              Math.max(
                n.activation,
                0.48 + centerBias * 0.12
              );
          }

        } else if (
          orbState === 'thinking'
        ) {
          const shieldR =
            Math.sqrt(
              n.hx * n.hx +
              n.hy * n.hy +
              n.hz * n.hz
            ) || 1;

          const isOuterShield =
            shieldR > RADIUS * 0.72;

          if (isOuterShield) {
            const shellPhi =
              Math.acos(
                clamp(
                  n.hz / shieldR,
                  -1,
                  1
                )
              );

            const wave01 =
              0.5 +
              0.5 *
                Math.sin(
                  shellPhi * 6.0 -
                  time * 5.0
                );

            // Bright crest travels with the physical outer-shell wave.
            const crest =
              Math.pow(
                wave01,
                5
              );

            n.activation =
              lerp(
                n.activation,
                0.06 +
                  crest * 0.72,
                0.16
              );

          } else {
            // Strengthen the inner orb so it remains readable under the
            // animated outer Thinking shell.
            const innerPulse =
              0.5 +
              0.5 *
                Math.sin(
                  time * 2.2 +
                  i * 0.035
                );

            n.activation =
              lerp(
                n.activation,
                0.09 +
                  innerPulse * 0.18,
                0.14
              );
          }

        } else if (
          orbState === 'speaking'
        ) {
          const ring =
            Math.floor(
              i /
              SPEAK_POINTS_PER_RING
            );

          const yNorm =
            mapVal(
              ring,
              0,
              SPEAK_RINGS - 1,
              -1,
              1
            );

          const fallback =
            isSpeaking
              ? 0.10
              : 0;

          const energy =
            Math.max(
              wordPulse,
              fallback
            );

          const band =
            0.5 +
            0.5 *
              Math.sin(
                yNorm *
                  Math.PI *
                  5 -
                time * 10
              );

          n.activation =
            lerp(
              n.activation,
              energy *
                (
                  0.08 +
                  band * 0.45
                ),
              0.18
            );

        } else {
          n.activation =
            lerp(
              n.activation,
              0,
              0.1
            );
        }

        const sx =
          (n.x + driftX) *
          currentGlobalScale;

        const sy =
          (n.y + driftY) *
          currentGlobalScale;

        const sz =
          (n.z + driftZ) *
          currentGlobalScale;

        const x1 =
          sx * cosY -
          sz * sinY;

        const z1 =
          sz * cosY +
          sx * sinY;

        const y2 =
          sy * cosX -
          z1 * sinX;

        const z2 =
          z1 * cosX +
          sy * sinX;

        const scale =
          fov / (fov + z2);

        const px =
          x1 * scale + cx;

        const py =
          y2 * scale + cy;

        projMap[i] = {
          idx: i,
          x: px,
          y: py,
          z: z2,
          scale,
          act: n.activation,
          alpha: n.alpha,
          isExtra: n.isExtra
        };
      }

      // ----------------------------------------------------------
      // CONNECTIONS
      // ----------------------------------------------------------
      function drawBaseTopology(opacityMultiplier = 1) {
        ctx.lineWidth = 1.0;

        for (const edge of baseEdges) {
          const p1 = projMap[edge.a];
          const p2 = projMap[edge.b];

          if (!p1 || !p2) continue;

          const avgZ =
            (p1.z + p2.z) / 2;

          const depthAlpha =
            mapVal(
              avgZ,
              -RADIUS * 0.4,
              RADIUS,
              0.45,
              0.02
            );

          if (depthAlpha <= 0) continue;

          const actHighlight =
            Math.max(
              p1.act,
              p2.act
            );

          const finalAlpha =
            Math.min(
              1,
              (
                depthAlpha +
                actHighlight * 0.8
              ) *
              edge.strength *
              opacityMultiplier
            );

          if (finalAlpha <= 0.01) continue;

          const r =
            Math.min(
              220,
              currentColor.r +
              actHighlight * 100
            );

          const g =
            Math.min(
              255,
              currentColor.g +
              actHighlight * 100
            );

          ctx.beginPath();
          ctx.moveTo(p1.x, p1.y);
          ctx.lineTo(p2.x, p2.y);

          if (spectrumMix > 0) {
            const c = spectrumRgb((p1.x + p2.x) / 2, (p1.y + p2.y) / 2, actHighlight);
            ctx.strokeStyle = blendRgba([r, g, 255], c, finalAlpha, finalAlpha * 1.9);
          } else {
            ctx.strokeStyle =
              `rgba(${r}, ${g}, 255, ${finalAlpha})`;
          }

          ctx.stroke();
        }
      }

      function drawStructuredTopology(
        edges,
        opacityMultiplier = 1,
        applySpeakingGlow = false
      ) {
        for (const edge of edges) {
          const p1 = projMap[edge.a];
          const p2 = projMap[edge.b];

          if (!p1 || !p2) continue;

          const endpointAlpha =
            Math.min(
              p1.alpha,
              p2.alpha
            );

          if (endpointAlpha < 0.03) continue;

          const avgZ =
            (p1.z + p2.z) / 2;

          let depthAlpha =
            mapVal(
              avgZ,
              -RADIUS,
              RADIUS,
              0.60,
              0.07
            );

          depthAlpha =
            clamp(
              depthAlpha,
              0.04,
              0.62
            );

          const act =
            Math.max(
              p1.act,
              p2.act
            );

          let alpha =
            (
              depthAlpha +
              act * 0.46
            ) *
            edge.strength *
            endpointAlpha *
            opacityMultiplier;

          if (applySpeakingGlow) {
            const wholeGlow =
              Math.max(
                wordPulse,
                isSpeaking ? 0.12 : 0
              );

            alpha +=
              wholeGlow *
              0.26 *
              edge.strength *
              opacityMultiplier;
          }

          alpha =
            clamp(
              alpha,
              0,
              0.88
            );

          if (alpha < 0.015) continue;

          const r =
            Math.min(
              238,
              currentColor.r +
              act * 95
            );

          const g =
            Math.min(
              255,
              currentColor.g +
              act * 110
            );

          ctx.beginPath();
          ctx.moveTo(p1.x, p1.y);
          ctx.lineTo(p2.x, p2.y);

          if (spectrumMix > 0) {
            const c = spectrumRgb((p1.x + p2.x) / 2, (p1.y + p2.y) / 2, act);
            ctx.strokeStyle = blendRgba([r, g, 255], c, alpha, alpha * 1.9);
          } else {
            ctx.strokeStyle =
              `rgba(${Math.round(r)}, ${Math.round(g)}, 255, ${alpha})`;
          }

          ctx.lineWidth =
            edge.strength > 0.5
              ? 1.0
              : 0.65;

          ctx.stroke();
        }
      }

      if (
        orbState === 'idle' ||
        orbState === 'listening'
      ) {
        drawBaseTopology(1);

      } else if (
        orbState === 'thinking'
      ) {
        drawStructuredTopology(
          shieldEdges,
          1,
          false
        );

      } else if (
        orbState === 'speaking'
      ) {
        drawStructuredTopology(
          speakingEdges,
          1,
          true
        );

      } else {
        // Critical smooth-return fix:
        // the Speaking lattice does not disappear on one frame.
        // It fades out while the Idle topology fades in.
        drawStructuredTopology(
          speakingEdges,
          1 - returnEase,
          false
        );

        drawBaseTopology(
          returnEase
        );
      }

      // ----------------------------------------------------------
      // DOTS
      // ----------------------------------------------------------
      const sortedNodes =
        [...projMap]
          .filter(Boolean)
          .sort(
            (a, b) =>
              b.z - a.z
          );

      for (
        const p of sortedNodes
      ) {
        if (
          p.alpha < 0.025
        ) {
          continue;
        }

        let depthAlpha =
          mapVal(
            p.z,
            -RADIUS,
            RADIUS,
            0.9,
            0.05
          );

        if (
          depthAlpha <= 0
        ) {
          continue;
        }

        let finalAlpha =
          Math.min(
            1,
            depthAlpha +
              p.act * 0.8
          ) *
          p.alpha;

        const speakingWholeGlow =
          orbState === 'speaking'
            ? Math.max(
                wordPulse,
                isSpeaking ? 0.12 : 0
              )
            : 0;

        if (orbState === 'speaking') {
          finalAlpha =
            Math.min(
              1,
              finalAlpha +
              speakingWholeGlow * 0.28
            );
        }

        if (
          finalAlpha <
          0.02
        ) {
          continue;
        }

        let rad =
          2.0 * p.scale *
          (
            1 +
            speakingWholeGlow * 0.28
          );

        let r =
          currentColor.r +
          speakingWholeGlow * 90;

        let g =
          currentColor.g +
          speakingWholeGlow * 110;

        let b =
          currentColor.b +
          speakingWholeGlow * 120;

        if (p.act > 0.05) {
          r +=
            p.act * 255;

          g +=
            p.act * 255;

          b +=
            p.act * 255;

          rad +=
            p.act * 1.5;
        }

        r =
          Math.min(255, r);

        g =
          Math.min(255, g);

        b =
          Math.min(255, b);

        ctx.beginPath();

        ctx.arc(
          p.x,
          p.y,
          rad,
          0,
          Math.PI * 2
        );

        if (spectrumMix > 0) {
          const c = spectrumRgb(p.x, p.y, p.act);
          ctx.fillStyle = blendRgba([r, g, b], c, finalAlpha, finalAlpha * 1.6);
          ctx.fill();
          // A hairline dark rim attached to each node (not a halo) keeps it readable on white backgrounds.
          ctx.lineWidth = 0.6;
          ctx.strokeStyle = `rgba(8, 12, 28, ${Math.min(0.6, finalAlpha * 0.55) * spectrumMix})`;
          ctx.stroke();
        } else {
          ctx.fillStyle =
            `rgba(
              ${Math.round(r)},
              ${Math.round(g)},
              ${Math.round(b)},
              ${finalAlpha}
            )`;

          ctx.fill();
        }

        if (
          p.z < -150 &&
          p.act <= 0.15
        ) {
          ctx.beginPath();

          ctx.arc(
            p.x,
            p.y,
            rad * 3,
            0,
            Math.PI * 2
          );

          ctx.fillStyle =
            `rgba(
              ${currentColor.r},
              ${currentColor.g},
              ${currentColor.b},
              ${finalAlpha * 0.18}
            )`;

          ctx.fill();
        }
      }

      // Switch to real Idle only after rendering the fully-eased final
      // return frame, so the following frame starts from the same look.
      if (
        orbState === 'returning' &&
        returnProgress >= 1
      ) {
        setOrbState('idle');
      }
    }

    function start() {
      // The overlay has its final CSS box before start() is called. Resolve the
      // backing store now, while the intro is still transparent, so the first
      // visible frame is native-DPR rather than a scaled bootstrap canvas.
      pendingResolutionSync = false;
      syncRenderResolution();
      isActive = true;
      if (!renderingSuspended && animationFrameId === null) {
        lastFrameTimestamp = 0;
        draw(performance.now());
      }
    }

    function stop() {
      isActive = false;
      if (animationFrameId !== null) cancelAnimationFrame(animationFrameId);
      animationFrameId = null;
      lastFrameTimestamp = 0;
    }

    document.addEventListener('visibilitychange', () => {
      renderingSuspended = document.visibilityState === 'hidden';
      if (renderingSuspended) {
        if (animationFrameId !== null) cancelAnimationFrame(animationFrameId);
        animationFrameId = null;
      } else if (isActive && animationFrameId === null) {
        lastFrameTimestamp = 0;
        draw(performance.now());
      }
    });

    function pulseSpeaking(strength = 0.5) {
      if (orbState !== 'speaking') return;
      audioSpeechEnergy = Math.min(
        Math.max(audioSpeechEnergy, Math.max(0, Number(strength) || 0)),
        1
      );
      audioEnergyUpdatedAt = performance.now();
    }

    function setSpeechEnergy(strength = 0) {
      window.JupiterDesktopAssistant?.energy?.('speaking', strength);   // marker 197: notch eyes
      audioSpeechEnergy = Math.min(Math.max(Number(strength) || 0, 0), 1);
      audioEnergyUpdatedAt = performance.now();
    }

    function setListeningEnergy(strength = 0) {
      window.JupiterDesktopAssistant?.energy?.('listening', strength);  // marker 197: notch eyes
      listeningInputEnergy = Math.min(Math.max(Number(strength) || 0, 0), 1);
      listeningEnergyUpdatedAt = performance.now();
    }

    window.JupiterNeuralOrb = {
      start,
      stop,
      // blendMs > 0 cross-fades speech blue <-> camera RGB inside the geometry (camera enter/exit transition).
      setSpectrum: (on, { blendMs = 0 } = {}) => {
        spectrumTarget = on ? 1 : 0;
        spectrumBlendMs = Math.max(0, Number(blendMs) || 0);
        if (!spectrumBlendMs) spectrumMix = spectrumTarget;
        spectrumMixAt = 0;
        spectrumAt = 0;
      },
      getSpectrum: () => spectrumTarget === 1,
      getSpectrumMix: () => spectrumMix,
      setState: setOrbState,
      getState: () => semanticOrbState,
      pulseSpeaking,
      setSpeechEnergy,
      setListeningEnergy
    };
})();

// The blob: a sphere pushed around by 3D noise in the vertex shader. Louder audio means
// bigger, faster, more detailed waves. Its colour eases between three palettes.

import * as THREE from "three";

const PALETTES = {
  idle: { core: "#7887ab", deep: "#0e1220", rim: "#c6d2ee" },
  user: { core: "#2a74ff", deep: "#04103a", rim: "#86bbff" },
  agent: { core: "#11cf86", deep: "#022e1c", rim: "#86ffca" },
};

// Simplex 3D noise by Ian McEwan and Stefan Gustavson (MIT licence).
const NOISE = /* glsl */ `
vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 mod289(vec4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 permute(vec4 x) { return mod289(((x * 34.0) + 10.0) * x); }
vec4 taylorInvSqrt(vec4 r) { return 1.79284291400159 - 0.85373472095314 * r; }

float snoise(vec3 v) {
  const vec2 C = vec2(1.0 / 6.0, 1.0 / 3.0);
  const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
  vec3 i = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);
  vec3 g = step(x0.yzx, x0.xyz);
  vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);
  vec3 x1 = x0 - i1 + C.xxx;
  vec3 x2 = x0 - i2 + C.yyy;
  vec3 x3 = x0 - D.yyy;
  i = mod289(i);
  vec4 p = permute(permute(permute(
            i.z + vec4(0.0, i1.z, i2.z, 1.0))
          + i.y + vec4(0.0, i1.y, i2.y, 1.0))
          + i.x + vec4(0.0, i1.x, i2.x, 1.0));
  float n_ = 0.142857142857;
  vec3 ns = n_ * D.wyz - D.xzx;
  vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);
  vec4 x = x_ * ns.x + ns.yyyy;
  vec4 y = y_ * ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);
  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);
  vec4 s0 = floor(b0) * 2.0 + 1.0;
  vec4 s1 = floor(b1) * 2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));
  vec4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw * sh.zzww;
  vec3 p0 = vec3(a0.xy, h.x);
  vec3 p1 = vec3(a0.zw, h.y);
  vec3 p2 = vec3(a1.xy, h.z);
  vec3 p3 = vec3(a1.zw, h.w);
  vec4 norm = taylorInvSqrt(vec4(dot(p0, p0), dot(p1, p1), dot(p2, p2), dot(p3, p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  vec4 m = max(0.5 - vec4(dot(x0, x0), dot(x1, x1), dot(x2, x2), dot(x3, x3)), 0.0);
  m = m * m;
  return 105.0 * dot(m * m, vec4(dot(p0, x0), dot(p1, x1), dot(p2, x2), dot(p3, x3)));
}
`;

const VERTEX = /* glsl */ `
uniform float uTime;
uniform float uLevel;
varying vec3 vNormal;
varying vec3 vViewPos;
varying float vDisp;

${NOISE}

float displace(vec3 n) {
  float t = uTime;
  float broad = snoise(n * 1.1 + vec3(0.0, t * 0.22, t * 0.15));
  float detail = snoise(n * 2.4 + vec3(t * 0.55, -t * 0.4, t * 0.45));
  float ripple = snoise(n * 4.6 + vec3(-t * 1.3, t * 1.0, 0.0));
  return broad * (0.08 + 0.12 * uLevel)
       + detail * (0.018 + 0.065 * uLevel)
       + ripple * (0.028 * uLevel * uLevel);
}

vec3 orthogonal(vec3 v) {
  return normalize(abs(v.x) > abs(v.z) ? vec3(-v.y, v.x, 0.0) : vec3(0.0, -v.z, v.y));
}

void main() {
  // Displace along the sphere normal, then rebuild the normal from two nearby points
  // so the lighting follows the waves.
  vec3 n = normalize(position);
  vec3 t = orthogonal(n);
  vec3 b = normalize(cross(n, t));
  const float e = 0.012;
  vec3 n1 = normalize(n + t * e);
  vec3 n2 = normalize(n + b * e);
  float d0 = displace(n);
  vec3 p0 = n * (1.0 + d0);
  vec3 p1 = n1 * (1.0 + displace(n1));
  vec3 p2 = n2 * (1.0 + displace(n2));

  vDisp = d0;
  vNormal = normalize(normalMatrix * cross(p1 - p0, p2 - p0));
  vec4 mv = modelViewMatrix * vec4(p0, 1.0);
  vViewPos = mv.xyz;
  gl_Position = projectionMatrix * mv;
}
`;

const FRAGMENT = /* glsl */ `
uniform vec3 uCore;
uniform vec3 uDeep;
uniform vec3 uRim;
uniform float uLevel;
varying vec3 vNormal;
varying vec3 vViewPos;
varying float vDisp;

void main() {
  vec3 N = normalize(vNormal);
  vec3 V = normalize(-vViewPos);
  float fresnel = pow(1.0 - clamp(dot(N, V), 0.0, 1.0), 2.4);

  vec3 key = normalize(vec3(-0.45, 0.8, 0.55));
  vec3 fill = normalize(vec3(0.75, -0.45, 0.35));
  float diffuse = clamp(dot(N, key), 0.0, 1.0);
  float back = clamp(dot(N, fill), 0.0, 1.0);
  float spec = pow(clamp(dot(N, normalize(key + V)), 0.0, 1.0), 28.0);

  vec3 col = mix(uDeep, uCore, 0.2 + 0.8 * diffuse);
  col += uRim * back * 0.18;
  col += uRim * fresnel * (0.85 + 0.9 * uLevel);
  col += uCore * smoothstep(0.02, 0.2, vDisp) * (0.12 + 0.4 * uLevel);
  col += spec * 0.16;

  gl_FragColor = vec4(col, 1.0);
  #include <colorspace_fragment>
}
`;

const easeToward = (rate, dt) => 1 - Math.exp(-rate * dt);

/**
 * Renders the blob into `canvas`. Every frame it calls `sample(seconds)`, which returns
 * `{ tone: "idle" | "user" | "agent", level: 0..1 }`, and eases toward it. It also sets the
 * --glow and --level CSS variables so the page's glow follows the blob.
 */
export class Blob {
  constructor(canvas, sample) {
    this.sample = sample;
    this.level = 0;
    this.time = 0;
    this.colors = {
      core: new THREE.Color(PALETTES.idle.core),
      deep: new THREE.Color(PALETTES.idle.deep),
      rim: new THREE.Color(PALETTES.idle.rim),
    };
    this.targets = Object.fromEntries(
      Object.entries(PALETTES).map(([tone, p]) => [
        tone,
        { core: new THREE.Color(p.core), deep: new THREE.Color(p.deep), rim: new THREE.Color(p.rim) },
      ]),
    );
    this.calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.root = document.documentElement.style;

    try {
      this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    } catch (err) {
      console.warn("WebGL is unavailable; showing the glow only.", err);
    }
    if (this.renderer) {
      this.renderer.setClearColor(0x000000, 0);
      this.scene = new THREE.Scene();
      this.camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100);
      this.uniforms = {
        uTime: { value: 0 },
        uLevel: { value: 0 },
        uCore: { value: this.colors.core },
        uDeep: { value: this.colors.deep },
        uRim: { value: this.colors.rim },
      };
      this.mesh = new THREE.Mesh(
        new THREE.IcosahedronGeometry(1, 64),
        new THREE.ShaderMaterial({
          uniforms: this.uniforms,
          vertexShader: VERTEX,
          fragmentShader: FRAGMENT,
        }),
      );
      this.scene.add(this.mesh);
      this.resize();
      window.addEventListener("resize", () => this.resize());
    }

    this.last = performance.now();
    const frame = (now) => {
      this.frame(Math.min((now - this.last) / 1000, 0.1), now / 1000);
      this.last = now;
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  resize() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    const aspect = w / h;
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(w, h, false);
    this.camera.aspect = aspect;
    // Pull back on narrow screens so the blob always fits the width.
    this.camera.position.z = 7.2 / Math.min(1, aspect * 1.3);
    this.camera.updateProjectionMatrix();
    // Sit slightly above centre, leaving room for the button.
    const visibleHeight = 2 * this.camera.position.z * Math.tan(THREE.MathUtils.degToRad(17.5));
    this.mesh.position.y = visibleHeight * 0.06;
  }

  frame(dt, seconds) {
    const { tone, level: target } = this.sample(seconds);

    // Fast attack, slower release, so speech reads as movement rather than flicker.
    const rate = target > this.level ? 14 : 4;
    this.level += (target - this.level) * easeToward(rate, dt);

    const palette = this.targets[tone] ?? this.targets.idle;
    const blend = easeToward(3.2, dt);
    this.colors.core.lerp(palette.core, blend);
    this.colors.deep.lerp(palette.deep, blend);
    this.colors.rim.lerp(palette.rim, blend);

    const glow = this.colors.core.getStyle(THREE.SRGBColorSpace).match(/\d+/g).slice(0, 3);
    this.root.setProperty("--glow", glow.join(", "));
    this.root.setProperty("--level", this.level.toFixed(3));

    if (!this.renderer) return;
    const speed = this.calm ? 0.3 : 0.5 + 1.3 * this.level;
    this.time += dt * speed;
    this.uniforms.uTime.value = this.time;
    this.uniforms.uLevel.value = this.calm ? this.level * 0.4 : this.level;
    this.mesh.rotation.y += dt * 0.08;
    this.mesh.rotation.x = Math.sin(seconds * 0.2) * 0.15;
    this.mesh.scale.setScalar(1 + this.level * 0.1);
    this.renderer.render(this.scene, this.camera);
  }
}

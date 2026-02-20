const C='algodomoiot-v1';
const S=['/','/control','/manifest.webmanifest','/icon.svg'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(S)));self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==C).map(x=>caches.delete(x)))));self.clients.claim();});
self.addEventListener('fetch',e=>{
const r=e.request,u=new URL(r.url);if(r.method!=='GET'||u.origin!==location.origin||u.pathname.startsWith('/api/'))return;
if(r.mode==='navigate'){e.respondWith(fetch(r).then(x=>{const c=x.clone();caches.open(C).then(k=>k.put(r,c));return x;}).catch(()=>caches.match(r).then(x=>x||caches.match('/'))));return;}
e.respondWith(caches.match(r).then(x=>x||fetch(r).then(y=>{const c=y.clone();caches.open(C).then(k=>k.put(r,c));return y;})));
});

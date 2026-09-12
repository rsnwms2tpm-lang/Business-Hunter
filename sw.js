const CACHE="business-hunter-v1";
const ASSETS=["./","./index.html","./manifest.webmanifest"];
self.addEventListener("install",e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(ASSETS))));
self.addEventListener("activate",e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener("fetch",e=>{
 if(e.request.mode==="navigate"){
  e.respondWith(fetch(e.request).then(r=>{const c=r.clone();caches.open(CACHE).then(cache=>cache.put("./index.html",c));return r}).catch(()=>caches.match("./index.html")));
 } else e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));
});
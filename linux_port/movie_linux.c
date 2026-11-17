// Linux stubs for H.264 (AVC) and AAC movie decoders (Workload-A uses VP9/AV1 WebM + Vorbis/KTSL2).
#include "dm.h"
#include "hle_movie_avc.h"
#include "android/aac_android.h"
#include <stdlib.h>

struct mavc { int red; };
mavc *mavc_new(void) { return calloc(1, sizeof(mavc)); }
void mavc_free(mavc *a) { free(a); }
void mavc_param_sets(mavc *a, const uint8_t *p, size_t n) { (void)a; (void)p; (void)n; }
void mavc_avcc(mavc *a, const uint8_t *p, size_t n) { (void)a; (void)p; (void)n; }
int mavc_decode(mavc *a, const uint8_t *p, int n, int64_t pts_us) {
    (void)p; (void)n; (void)pts_us;
    if (a && !a->red++) LOG("[RED] movie: H.264 (AVC) decoder not wired on Linux yet");
    return 0;
}
int mavc_take(mavc *a, int flush, uint8_t *nv12, int w, int h, int64_t *pts_us) {
    (void)a; (void)flush; (void)nv12; (void)w; (void)h; (void)pts_us;
    return 0;
}
int mavc_queued(const mavc *a) { (void)a; return 0; }
void mavc_set_rgba(mavc *a) { (void)a; }
void mavc_drop(mavc *a) { (void)a; }

struct maac { int unused; };
maac *maac_new(int *rate, int *ch, const uint8_t *asc, int asc_n) {
    (void)asc; (void)asc_n;
    if (rate && *rate <= 0) *rate = 48000;
    if (ch && *ch <= 0) *ch = 2;
    LOG("[RED] movie: AAC decoder not wired on Linux yet, outputting silence");
    return NULL;
}
void maac_free(maac *m) { free(m); }
int maac_decode(maac *m, const uint8_t *p, int n, int64_t pts_us, int16_t *out, int max_vals) {
    (void)m; (void)p; (void)n; (void)pts_us; (void)out; (void)max_vals;
    return 0;
}

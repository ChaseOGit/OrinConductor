# Linux aarch64 Makefile for OrinNative (dm/host) with full Vulkan replay, zstd, dav1d, and SDL3
GAME ?= 0400000000010000
GAME_DIR := $(if $(wildcard ../games/$(GAME)/game.tsv),../games/$(GAME),$(shell python3 ../games/game.py link $(GAME) gen/$(GAME)/profile >&2 && echo gen/$(GAME)/profile))
GAME_H = gen/$(GAME)/game_profile.h
SEED_H = gen/$(GAME)/vk_prewarm_seed.h
GAME_STAMP = gen/game.stamp

SDL3_CFLAGS := $(shell pkg-config --cflags sdl3 2>/dev/null) -I/usr/local/include
SDL3_LIBS   := $(shell pkg-config --libs sdl3 2>/dev/null || echo "-L/usr/local/lib -lSDL3") -Wl,-rpath,/usr/local/lib -lX11

CC ?= clang
HOST_CFLAGS = -O2 -g -mcpu=native -fno-pie -D_GNU_SOURCE -DDM_NUP_ZSTD -DNX_COOP_VENDOR=0 -DNX_LSFG \
	-Wall -Wno-unused-function -Wno-format \
	-I. -Iplat -I../gfx/nvn/include -I../third_party/vulkan-headers/include \
	-I$(dir $(GAME_H)) -I$(VPX) -I$(VPX)/config -I$(DAV1D)/include -I../third_party/stb_vorbis $(SDL3_CFLAGS)

# plat/plat_linux.c MUST be first so dm_linux_tls_slot2 lands at tpidr_el0 + 0x10!
PLAT_FIRST = plat/plat_linux.c
HOST_REST = main.c loader.c hle.c crash.c probe.c thread.c save_sync.c memstat_linux.c movie_linux.c front_linux.c \
	$(filter-out hle_movie_avc.c,$(wildcard hle_*.c)) \
	android/dkb_wide.c android/mkw_wide.c android/oot_wide.c android/pkp_aspect.c \
	entry.S linux_auto_stubs.S

GFX = $(sort $(wildcard ../gfx/*.c ../gfx/nvn/*.c ../gfx/nvn/*.S ../gfx/tex/*.c ../gfx/vk_android/*.c ../gfx/vk_android/lsfg/*.c))
GFX_H = $(sort $(wildcard ../gfx/nvn/*.h ../gfx/nvn/*.inc ../gfx/nvn/include/*.h ../gfx/tex/*.h ../gfx/vk_android/*.h ../gfx/vk_android/*.inc ../gfx/vk_android/lsfg/*.h))
HOST_HDRS = $(wildcard dm.h plat/plat.h hle.h romfs_image.h nup_image.h save_sync.h dm_input.h pad_slots.h hle_haptics.h motion_fusion.h motion_pro.h pad_hid.h front.h android/*.h)

HOST_ALL = $(PLAT_FIRST) $(HOST_REST) $(GFX)
HOST_OBJ = $(patsubst %.c,gen/host/%.o,$(patsubst %.S,gen/host/%.s.o,$(subst ../,up_,$(HOST_ALL))))

VPX = ../third_party/libvpx
VPX_SRC = $(sort $(shell find $(VPX) -name '*.c'))
VPX_OBJ = $(patsubst $(VPX)/%.c,gen/vpx/%.o,$(VPX_SRC))
VPX_LIB = gen/vpx/libvpx.a

ZSTD = ../third_party/zstd
ZSTD_LIB = gen/zstd/libzstddec.a

DAV1D = ../third_party/dav1d
DAV1D_C = $(filter-out %_tmpl.c,$(wildcard $(DAV1D)/src/*.c $(DAV1D)/config/tmpl/*.c)) $(DAV1D)/src/arm/cpu.c
DAV1D_S = $(filter-out %_tmpl.S %/util.S,$(wildcard $(DAV1D)/src/arm/64/*.S))
DAV1D_OBJ = $(patsubst $(DAV1D)/%.c,gen/dav1d/%.o,$(DAV1D_C)) $(patsubst $(DAV1D)/%.S,gen/dav1d/%.s.o,$(DAV1D_S))
DAV1D_LIB = gen/dav1d/libdav1d.a
DAV1D_INC = -I../../android/host/dav1d_config -I$(DAV1D) -I$(DAV1D)/include -I$(DAV1D)/config

.DEFAULT_GOAL := dmhost

gen/vpx/%.o: $(VPX)/%.c $(wildcard $(VPX)/config/*.h)
	@mkdir -p $(dir $@)
	$(CC) -O2 -w -fno-pie -I$(VPX) -I$(VPX)/config -c $< -o $@

$(VPX_LIB): $(VPX_OBJ)
	@mkdir -p $(dir $@)
	rm -f $@ && ar rcs $@ $^

$(ZSTD_LIB): $(ZSTD)/zstddeclib.c
	@mkdir -p $(dir $@) gen/zstd
	$(CC) -O2 -w -fno-pie -c $< -o gen/zstd/zstddeclib.o
	rm -f $@ && ar rcs $@ gen/zstd/zstddeclib.o

gen/dav1d/%.o: $(DAV1D)/%.c
	@mkdir -p $(dir $@)
	$(CC) -O2 -w -fno-pie -D_GNU_SOURCE $(DAV1D_INC) -c $< -o $@

gen/dav1d/%.s.o: $(DAV1D)/%.S
	@mkdir -p $(dir $@)
	$(CC) -O2 -w -fno-pie -D_GNU_SOURCE $(DAV1D_INC) -c $< -o $@

$(DAV1D_LIB): $(DAV1D_OBJ)
	@mkdir -p $(dir $@)
	rm -f $@ && ar rcs $@ $^

$(GAME_H): $(GAME_DIR)/game.tsv ../games/game.py FORCE
	@mkdir -p $(dir $@)
	@python3 ../games/game.py header $(GAME) $@

$(SEED_H): $(GAME_DIR)/game.tsv ../games/game.py FORCE
	@mkdir -p $(dir $@)
	@python3 ../games/game.py seed $(GAME) $@

$(GAME_STAMP): FORCE
	@mkdir -p gen
	@echo $(GAME) | cmp -s - $@ || echo $(GAME) > $@

gen/host/up_%.o: ../%.c $(GAME_H) $(SEED_H) $(GAME_STAMP) $(HOST_HDRS) $(GFX_H)
	@mkdir -p $(dir $@)
	$(CC) $(HOST_CFLAGS) -c $< -o $@

gen/host/up_%.s.o: ../%.S $(GAME_STAMP)
	@mkdir -p $(dir $@)
	$(CC) $(HOST_CFLAGS) -c $< -o $@

gen/host/%.o: %.c $(GAME_H) $(SEED_H) $(GAME_STAMP) $(HOST_HDRS) $(GFX_H)
	@mkdir -p $(dir $@)
	$(CC) $(HOST_CFLAGS) -c $< -o $@

gen/host/%.s.o: %.S $(GAME_STAMP)
	@mkdir -p $(dir $@)
	$(CC) $(HOST_CFLAGS) -c $< -o $@

dmhost: $(HOST_OBJ) $(VPX_LIB) $(ZSTD_LIB) $(DAV1D_LIB)
	$(CC) -no-pie -fuse-ld=mold -rdynamic -Wl,--no-undefined -o $@ $(HOST_OBJ) $(VPX_LIB) $(ZSTD_LIB) $(DAV1D_LIB) -ldl -lpthread -lm -lz -lvulkan $(SDL3_LIBS)
	@mkdir -p build && cp -f dmhost build/orin_host

.PHONY: FORCE clean
FORCE:
clean:
	rm -rf gen dmhost build/orin_host

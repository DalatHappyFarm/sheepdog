// Package sheepdog holds the files the server serves, embedded in the binary.
// web/ is shared with the Python server (see web/README.md); brand/ is the fixed list the server whitelists.
package sheepdog

import (
	_ "embed"
)

// The page files, read once at start by the server.
var (
	//go:embed web/board.html
	BoardHTML string
	//go:embed web/login.html
	LoginHTML string
	//go:embed web/arch.html
	ArchHTML string
	//go:embed web/arch-diagram.html
	ArchDiagramHTML string
	//go:embed web/base.css
	BaseCSS string
	//go:embed web/theme.js
	ThemeJS string

	//go:embed brand/icon-126.png
	icon126 string
	//go:embed brand/icon-64.png
	icon64 string
	//go:embed brand/favicon-32.png
	favicon32 string
	//go:embed brand/hero.png
	hero string
)

// Brand returns the bytes of one of the four public brand PNGs (never any other file).
func Brand(name string) (string, bool) {
	switch name {
	case "icon-126.png":
		return icon126, true
	case "icon-64.png":
		return icon64, true
	case "favicon-32.png":
		return favicon32, true
	case "hero.png":
		return hero, true
	}
	return "", false
}

"""Tests para :mod:`core.tagger`.

``safe_dirname`` es lógica pura (sin IO). Los tests de lectura/escritura
usan el **ID3 path** (MP3/WAV/AIFF), porque mutagen puede crear un header
ID3 sobre un archivo vacío sin necesitar audio real — esto da un
round-trip verdadero del read/write para esa familia de contenedor,
incluyendo el manejo especial de WAV con ID3 directo.

Los paths FLAC y MP4 **no** se prueban aquí: mutagen requiere streams
válidos para abrir esos formatos (no se pueden crear sintéticos sin
audio real ni una dep extra como ``soundfile``). Esos paths se validan
manualmente con la biblioteca del usuario en la fase final. La lógica de
dispatch por extensión es paralela entre formatos, así que el ID3 test
ejercita los contratos sin perder cobertura significativa.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.tagger import (
    TrackTags,
    read_tags,
    safe_dirname,
    write_genre,
    write_genre_and_comment,
)


# --------------------------------------------------------------------------- #
# safe_dirname (pura, sin IO)
# --------------------------------------------------------------------------- #

class TestSafeDirname:
    @pytest.mark.parametrize("source,expected", [
        ("Tech House", "Tech House"),
        # `/` se reemplaza por ` - ` para preservar el nombre compound
        ("Disco / Nu-Disco", "Disco - Nu-Disco"),
        ("Minimal / Deep Tech", "Minimal - Deep Tech"),
        # Múltiples `/` en cascada
        ("A / B / C", "A - B - C"),
        # Whitespace múltiple se colapsa
        ("  Tech    House  ", "Tech House"),
        ("Tech\t\tHouse", "Tech House"),
        # Strings normales sin cambios
        ("Afro House", "Afro House"),
    ])
    def test_normal_cases(self, source, expected):
        assert safe_dirname(source) == expected

    @pytest.mark.parametrize("char", ["<", ">", ":", '"', "\\", "|", "?", "*"])
    def test_prohibited_chars_removed(self, char):
        """Caracteres prohibidos en Windows/exFAT se quitan."""
        result = safe_dirname(f"foo{char}bar")
        assert char not in result
        assert result == "foobar"

    def test_all_prohibited_chars_together(self):
        assert safe_dirname('a<>:"\\|?*b') == "ab"

    @pytest.mark.parametrize("source", ["", "   ", "\t\n"])
    def test_empty_or_only_whitespace_yields_sentinel(self, source):
        """String vacío o solo whitespace devuelve `_SIN_NOMBRE`."""
        assert safe_dirname(source) == "_SIN_NOMBRE"

    def test_only_prohibited_chars_yields_sentinel(self):
        assert safe_dirname('<>:"\\|?*') == "_SIN_NOMBRE"

    def test_only_slashes_becomes_dashes_not_sentinel(self):
        """`//` se traduce a `- -` (cada `/` → ` - `, whitespace colapsado,
        strip). NO se convierte en `_SIN_NOMBRE` porque tras sanitizar el
        resultado no queda vacío."""
        assert safe_dirname("//") == "- -"
        assert safe_dirname("/") == "-"

    def test_preserves_unicode(self):
        """Caracteres unicode válidos (acentos, ñ, etc.) se preservan."""
        assert safe_dirname("Música Electrónica") == "Música Electrónica"

    def test_combined_prohibited_and_slash(self):
        """Mix de `/` (se traduce) y prohibidos (se eliminan)."""
        assert safe_dirname('A / B<>C') == "A - BC"


# --------------------------------------------------------------------------- #
# TrackTags dataclass
# --------------------------------------------------------------------------- #

class TestTrackTagsDataclass:
    def test_defaults_empty_strings(self):
        t = TrackTags()
        assert t.genre == ""
        assert t.comment == ""

    def test_construction_with_values(self):
        t = TrackTags(genre="Tech House", comment="Energia: ALTA (6.0/9)")
        assert t.genre == "Tech House"
        assert t.comment == "Energia: ALTA (6.0/9)"

    def test_equality(self):
        a = TrackTags(genre="X", comment="Y")
        b = TrackTags(genre="X", comment="Y")
        assert a == b


# --------------------------------------------------------------------------- #
# Comportamiento defensivo de read_tags
# --------------------------------------------------------------------------- #

class TestReadTagsDefensive:
    def test_nonexistent_file_returns_empty(self, tmp_path: Path):
        """Archivo que no existe: nunca lanza, devuelve TrackTags vacío."""
        result = read_tags(tmp_path / "no_existe.flac")
        assert result == TrackTags()

    def test_unsupported_extension_returns_empty(self, tmp_path: Path):
        """Extensión no soportada: devuelve TrackTags vacío sin lanzar."""
        f = tmp_path / "archivo.xyz"
        f.write_bytes(b"contenido cualquiera")
        assert read_tags(f) == TrackTags()

    def test_corrupt_mp3_returns_empty(self, tmp_path: Path):
        """MP3 sin header ID3: read_id3 cacha ID3NoHeaderError y devuelve vacío."""
        f = tmp_path / "corrupt.mp3"
        f.write_bytes(b"no soy un MP3 valido")
        assert read_tags(f) == TrackTags()

    def test_ogg_returns_empty_no_write_support(self, tmp_path: Path):
        """OGG está en AUDIO_EXTS para enumeración pero no se lee/escribe."""
        f = tmp_path / "x.ogg"
        f.write_bytes(b"fake ogg")
        assert read_tags(f) == TrackTags()


# --------------------------------------------------------------------------- #
# Errores de escritura: formatos no soportados / paths inválidos
# --------------------------------------------------------------------------- #

class TestWriteErrors:
    def test_write_genre_unsupported_extension(self, tmp_path: Path):
        f = tmp_path / "x.xyz"
        f.write_bytes(b"")
        ok, motivo = write_genre(f, "Tech House")
        assert not ok
        assert "no soportado" in motivo
        assert ".xyz" in motivo

    def test_write_genre_and_comment_unsupported(self, tmp_path: Path):
        f = tmp_path / "x.xyz"
        f.write_bytes(b"")
        ok, motivo = write_genre_and_comment(f, "g", "c")
        assert not ok
        assert "no soportado" in motivo

    def test_write_ogg_returns_not_supported(self, tmp_path: Path):
        """OGG no se escribe (paridad con scripts originales)."""
        f = tmp_path / "x.ogg"
        f.write_bytes(b"")
        ok, motivo = write_genre(f, "Tech House")
        assert not ok and ".ogg" in motivo

        ok, motivo = write_genre_and_comment(f, "g", "c")
        assert not ok and ".ogg" in motivo


# --------------------------------------------------------------------------- #
# Round-trip ID3 (MP3) — write + read sobre el mismo archivo
# --------------------------------------------------------------------------- #

class TestID3RoundTrip:
    """ID3 path completo: escribir tags sobre un archivo vacío y releerlos.

    Esto ejercita la creación de header ID3 desde cero (rama
    ``ID3NoHeaderError → audio = ID3()`` del write), el ``setall('TCON')``,
    el ``COMM:energia:eng``, y el filtrado de COMM por prefijo del read.
    Como la lógica de dispatch por extensión es paralela entre familias,
    estos tests cubren el contrato general del módulo.
    """

    def test_write_genre_then_read(self, tmp_path: Path):
        f = tmp_path / "t.mp3"
        f.write_bytes(b"")
        ok, motivo = write_genre(f, "Tech House")
        assert ok, motivo

        tags = read_tags(f)
        assert tags.genre == "Tech House"
        assert tags.comment == ""  # write_genre NO escribe comment

    def test_write_genre_and_comment_then_read(self, tmp_path: Path):
        f = tmp_path / "t.mp3"
        f.write_bytes(b"")
        ok, _ = write_genre_and_comment(
            f, "Tech House / ALTA",
            "Energia: ALTA (6.0/9) | Genero: Tech House 63%",
        )
        assert ok

        tags = read_tags(f)
        assert tags.genre == "Tech House / ALTA"
        assert tags.comment == "Energia: ALTA (6.0/9) | Genero: Tech House 63%"

    def test_write_genre_preserves_existing_comment(self, tmp_path: Path):
        """``write_genre`` solo toca TCON, no debe borrar COMM."""
        f = tmp_path / "t.mp3"
        f.write_bytes(b"")
        write_genre_and_comment(f, "Tech House", "Energia: ALTA (6.0/9)")

        # Ahora actualizamos solo el genre (escenario de enrich)
        ok, _ = write_genre(f, "Tech House / ALTA")
        assert ok

        tags = read_tags(f)
        assert tags.genre == "Tech House / ALTA"
        # El comment NO debe haberse perdido
        assert tags.comment == "Energia: ALTA (6.0/9)"

    def test_write_genre_and_comment_overwrites_only_energia_comm(
        self, tmp_path: Path,
    ):
        """``write_genre_and_comment`` borra solo COMM:energia:eng, preserva
        otros COMM (notas del usuario en otros idiomas/descripciones)."""
        from mutagen.id3 import ID3, COMM

        f = tmp_path / "t.mp3"
        f.write_bytes(b"")
        # Pre-poblar con un COMM del usuario (otro desc, otro lang)
        audio = ID3()
        audio.add(COMM(
            encoding=3, lang="spa", desc="nota_usuario",
            text=["Esta es mi nota personal"],
        ))
        audio.save(str(f))

        # Llamamos write_genre_and_comment
        ok, _ = write_genre_and_comment(
            f, "Tech House", "Energia: ALTA (6.0/9)",
        )
        assert ok

        # Releer y verificar que la nota del usuario sigue ahí
        audio = ID3(str(f))
        user_notes = [
            k for k in audio.keys()
            if k.startswith("COMM") and "nota_usuario" in k
        ]
        assert len(user_notes) == 1, f"nota del usuario perdida: {list(audio.keys())}"
        # Y que se añadió el COMM:energia:eng
        energia_notes = [
            k for k in audio.keys()
            if k.startswith("COMM") and "energia" in k
        ]
        assert len(energia_notes) == 1

    def test_read_filters_comm_by_energia_prefix(self, tmp_path: Path):
        """read_tags devuelve el COMM cuyo texto empieza con 'Energia:',
        ignorando otros COMM (notas del usuario)."""
        from mutagen.id3 import ID3, COMM, TCON

        f = tmp_path / "t.mp3"
        f.write_bytes(b"")
        audio = ID3()
        audio.add(TCON(encoding=3, text=["Tech House"]))
        # Primero un COMM que NO es de energia
        audio.add(COMM(
            encoding=3, lang="spa", desc="nota",
            text=["nota libre del usuario"],
        ))
        # Después el de energia
        audio.add(COMM(
            encoding=3, lang="eng", desc="energia",
            text=["Energia: ALTA (6.0/9) | Genero: Tech House"],
        ))
        audio.save(str(f))

        tags = read_tags(f)
        assert tags.genre == "Tech House"
        # read_tags filtra por prefijo "Energia:" — toma el COMM correcto
        assert tags.comment.startswith("Energia:")
        assert "ALTA" in tags.comment

    def test_round_trip_preserves_arousal_raw_decimals(self, tmp_path: Path):
        """El comment con arousal '6.10' se preserva textual en el round-trip
        (no se convierte a '6.1' por mutagen)."""
        f = tmp_path / "t.mp3"
        f.write_bytes(b"")
        write_genre_and_comment(f, "X / ALTA", "Energia: ALTA (6.10/9)")

        tags = read_tags(f)
        assert "6.10" in tags.comment  # mutagen no toca el string


# --------------------------------------------------------------------------- #
# Comportamiento de read_tags por extensión (paths no testados con audio real)
# --------------------------------------------------------------------------- #

class TestReadTagsByExtension:
    """Verifica que read_tags despacha por extensión sin lanzar.

    Para FLAC/MP4 sin audio real, mutagen falla al abrir — el `except`
    defensivo cacha y devuelve TrackTags() vacío. Esto cubre la rama de
    error de cada formato sin necesitar audio.
    """

    @pytest.mark.parametrize("ext", [".flac", ".mp4", ".m4a", ".aac"])
    def test_invalid_container_returns_empty(self, tmp_path: Path, ext: str):
        f = tmp_path / f"bad{ext}"
        f.write_bytes(b"no soy un container valido")
        assert read_tags(f) == TrackTags()

    def test_wav_uses_id3_path(self, tmp_path: Path):
        """WAV se maneja con ID3 directo (no MutagenFile)."""
        f = tmp_path / "t.wav"
        f.write_bytes(b"")
        ok, _ = write_genre(f, "Tech House")
        assert ok  # write debería funcionar igual que MP3

        tags = read_tags(f)
        assert tags.genre == "Tech House"

    def test_aiff_uses_id3_path(self, tmp_path: Path):
        """AIFF también con ID3 directo (paralelo a WAV)."""
        f = tmp_path / "t.aiff"
        f.write_bytes(b"")
        ok, _ = write_genre(f, "Deep House")
        assert ok

        tags = read_tags(f)
        assert tags.genre == "Deep House"

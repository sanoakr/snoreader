import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '../api/client';

export function useGenres() {
  return useQuery({ queryKey: ['genres'], queryFn: api.getGenres, staleTime: 60_000 });
}

// ジャンル定義を変えると既存記事が再分類されるので、記事側のキャッシュも捨てる
function useInvalidateGenreDefs() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ['genres'] });
    qc.invalidateQueries({ queryKey: ['genre-counts'] });
    qc.invalidateQueries({ queryKey: ['articles'] });
  };
}

export function useCreateGenre() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({
    mutationFn: (body: { key: string; label_ja: string; priority: number }) => api.createGenre(body),
    onSuccess: invalidate,
  });
}

export function useUpdateGenre() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number; label_ja?: string; priority?: number }) =>
      api.updateGenre(id, body),
    onSuccess: invalidate,
  });
}

export function useDeleteGenre() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({ mutationFn: (id: number) => api.deleteGenre(id), onSuccess: invalidate });
}

export function useCreateGenreRule() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({
    mutationFn: (body: { tag: string; genre_id: number; is_generic: boolean }) =>
      api.createGenreRule(body),
    onSuccess: invalidate,
  });
}

export function useDeleteGenreRule() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({ mutationFn: (id: number) => api.deleteGenreRule(id), onSuccess: invalidate });
}

-- semantic-crosscheck: foreign keys proposed by shapes with fk_ddl = "review_only" (generated from crosscheck.toml).
-- FOR REVIEW ONLY: the tool never executes this file; a person decides whether to apply it.
-- NOT VALID adds a constraint without checking existing rows. Fix the rows listed by the shape's view first,
-- then run the commented VALIDATE CONSTRAINT. The referenced key needs a primary key or unique constraint.

-- shapes[0] child_exists: violating rows in sxc_out.v_shape_child_exists
ALTER TABLE "sxc_demo"."bom_line" ADD CONSTRAINT "sxc_fk_child_exists" FOREIGN KEY ("child_id")
  REFERENCES "sxc_demo"."part" ("part_id") NOT VALID;
-- ALTER TABLE "sxc_demo"."bom_line" VALIDATE CONSTRAINT "sxc_fk_child_exists";
